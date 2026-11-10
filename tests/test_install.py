# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""install.sh, the one-line installer, run through a pipe as `curl … | bash` runs it: against a local repository with
release tags, with stub `brew`, `gh`, `uname`, `sysctl` and `sw_vers` first on PATH, a temporary HOME, and the folders
where it looks for Homebrew moved into the temporary folder (`DEVKIT_TEST_BREW_PATHS`, a test hook of the installer).
Nothing here touches Homebrew, the network, ~/.claude, ~/.codex or the shell profile. The repository's `bin/dev` is a
stand-in that records how the setup wizard was started and, with the real devkit library, whether the wizard would ask
its questions.
"""
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sandbox  # noqa: E402  (this Mac's codex, claude, brew and gh stay out of reach of every process)

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
LIB = str(ROOT / "lib")
TROUBLESHOOTING = ROOT / "docs" / "troubleshooting.md"


def apple_silicon() -> bool:
    """Whether this computer has Apple silicon, as macOS's own /usr/sbin/sysctl says (1, also under Rosetta)."""
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "1"


def heading_anchors(markdown: Path) -> set:
    """The anchors GitHub gives the headings of a Markdown page (not the `#` comments in its code blocks)."""
    anchors, fenced = set(), False
    for ln in markdown.read_text().splitlines():
        if ln.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", ln):
            anchors.add(re.sub(r"[^\w\- ]", "", ln.lstrip("#").strip().lower()).replace(" ", "-"))
    return anchors

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ada",
               GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada", GIT_COMMITTER_EMAIL="ada@example.com")

STUBS = {
    "uname": """#!/bin/sh
echo "uname $*" >> "$STUB_LOG"
case "$1" in -m) echo "${STUB_ARCH:-arm64}" ;; *) echo "${STUB_OS:-Darwin}" ;; esac
""",
    # hw.optional.arm64 is 1 on Apple silicon, also in a terminal under Rosetta (STUB_ROSETTA=1), and unknown on Intel
    "sysctl": """#!/bin/sh
echo "sysctl $*" >> "$STUB_LOG"
case "${STUB_ARCH:-arm64}/${STUB_ROSETTA:-}" in arm64/* | */1) arm64=1 ;; *) arm64="" ;; esac
if [ "$*" = "-n hw.optional.arm64" ] && [ -n "$arm64" ]; then echo 1; exit 0; fi
echo "sysctl: unknown oid '$2'" >&2
exit 1
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
    # `gh auth login` signs in; STUB_GH_LOGIN=fail-once fails the first time with an error line, =hang waits like a
    # browser page nobody finishes (exec, so that stopping it stops the whole stand-in)
    "gh": """#!/bin/sh
if [ -t 0 ]; then input=terminal; elif [ -p /dev/stdin ]; then input=pipe; else input=none; fi
echo "gh $* ($input)" >> "$STUB_LOG"
case "$1 $2 $3" in
  "auth login --help") echo "  -c, --clipboard   Copy one-time OAuth device code to clipboard"; exit 0 ;;
esac
case "$1 $2" in
  "auth status") [ -f "$STUB_GH_SIGNED_IN" ] ;;
  "auth login")
    echo "! First copy your one-time code: 4F2A-9C1D" >&2
    case "${STUB_GH_LOGIN:-}" in
      hang) exec sleep 30 ;;
      fail-once) if [ ! -f "$STUB_GH_SIGNED_IN.tried" ]; then touch "$STUB_GH_SIGNED_IN.tried"
                   printf 'Opening github.com in your browser.\nerror: device code expired\n' >&2; exit 1; fi ;;
    esac
    touch "$STUB_GH_SIGNED_IN" ;;
  "api user") echo ada ;;
  "--version ") echo "gh version 2.80.0 (2026-10-01)" ;;
esac
""",
}

# The stand-in for devkit's bin/dev in the fixture repository: it records how `dev setup` was started.
DEV = """#!/usr/bin/env python3
import json, os, select, sys
home = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
rec = {"argv": sys.argv[1:], "stdin_tty": sys.stdin.isatty(), "home": home,
       "python": os.environ.get("STUB_PYTHON", ""), "version": open(os.path.join(home, "VERSION")).read().strip(),
       "installer": os.environ.get("DEVKIT_INSTALLER", "")}
# what a program that watches its input with kqueue (Claude Code's runtime) gets from this stdin
try:
    select.kqueue().control([select.kevent(0, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0, 0)
    rec["stdin_kqueue"] = "ok"
except (AttributeError, OSError) as e:
    rec["stdin_kqueue"] = getattr(e, "strerror", None) or type(e).__name__
if os.environ.get("REAL_LIB"):
    sys.path.insert(0, os.environ["REAL_LIB"])
    from devkit import tui, wizard
    rec["wizard_asks"] = tui.available() and not wizard.Prompter("--yes" in sys.argv).yes
with open(os.environ["SETUP_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\\n")
sys.exit(int(os.environ.get("STUB_SETUP_RC") or 0))
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
        # Where the installer looks for a brew that is not on PATH: stand-ins for /opt/homebrew and /usr/local, empty
        # until a test puts brew there, so that no test reaches this Mac's own Homebrew.
        self.brew_paths = (root / "opt" / "homebrew" / "bin" / "brew", root / "usr" / "local" / "bin" / "brew")

    def move_brew(self, to: Path) -> dict:
        """Take brew off PATH and put it at `to`, one of `brew_paths`, with a python3 next to it, as Homebrew has.
        Returns the environment that makes that brew report its prefix."""
        prefix = to.parent.parent
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.stubs / "brew"), str(to))
        shutil.copy(str(self.prefix / "bin" / "python3"), str(prefix / "bin" / "python3"))
        return {"STUB_PREFIX": str(prefix)}

    def env(self, **extra) -> dict:
        env = dict(GIT_ENV, HOME=str(self.home), PATH=f"{self.stubs}:/usr/bin:/bin", TERM="xterm-256color",
                   LANG="en_US.UTF-8", STUB_LOG=str(self.log), STUB_PREFIX=str(self.prefix),
                   STUB_GH_SIGNED_IN=str(self.signed_in), SETUP_LOG=str(self.setup_log), REAL_LIB=LIB,
                   DEVKIT_REPO=f"file://{self.remote}", DEVKIT_TEST_BREW_PATHS=":".join(map(str, self.brew_paths)),
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
        # the stubs are this Mac's only tools: one a test removed (brew) stays absent in devkit's processes too
        return _sandbox.env(env, stubs=[self.stubs], run=False)

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
        rc, out = self.m.pipe()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.release(), "v26.11.10")  # numbers, not text: 10 > 9 > 2; not main's newer commit
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.m.devkit), "symbolic-ref", "-q", "HEAD"],
                                           env=GIT_ENV, capture_output=True).returncode, 0)  # detached at the tag
        (setup,) = self.m.setups()
        self.assertEqual((setup["home"], setup["version"], setup["python"]),
                         (str(self.m.devkit.resolve()), "26.11.10", "homebrew"))
        # the wizard learns the local installer, for its "run it again" lines
        self.assertEqual(setup["installer"], str(self.m.devkit / "install.sh"))
        self.assertEqual(out.splitlines()[0], "devkit installer")
        headings = [ln for ln in out.splitlines() if re.match(r"\d of 5  ", ln)]
        self.assertEqual(headings, ["1 of 5  This Mac", "2 of 5  Homebrew, git, Python and the GitHub CLI",
                                    "3 of 5  devkit", "4 of 5  GitHub sign-in", "5 of 5  Your setup"])
        self.assertIn("✓ macOS 26.1 on Apple silicon", out)
        self.assertNotIn("Intel", out)  # on Apple silicon, no Intel warning and no Intel claim
        self.assertIn("✓ Homebrew 4.6.0 in", out)
        self.assertIn("✓ GitHub CLI 2.80.0", out)
        self.assertIn("✓ Downloaded devkit 26.11.10 to ~/Development/fireflysoftwaresolutions/devkit", out)
        self.assertIn("✓ Signed in to GitHub as ada", out)
        self.assertFalse([c for c in self.m.calls() if c.startswith("brew install")])

    def test_without_a_terminal_it_starts_at_once_with_no_welcome_question(self):
        """INSTALL-AC-11: no terminal on stdin or as a device: no welcome to wait at, straight to the first step."""
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Press Enter to start", out)
        self.assertNotIn("Nothing has changed yet", out)
        self.assertEqual(out.splitlines()[:3], ["devkit installer", "", "1 of 5  This Mac"])

    def test_after_the_wizard_ran_the_installer_adds_nothing_and_keeps_its_exit_code(self):
        """INSTALL-AC-42: the wizard's summary is the last thing on screen, whatever its outcome (R25, R27)."""
        for code in ("0", "1", "2", "130"):
            with self.subTest(wizard=code):
                rc, out = self.m.pipe(STUB_SETUP_RC=code)
                self.assertEqual(rc, int(code), out)
                self.assertNotIn("Summary", out)
                self.assertNotIn("exit code", out)
                last = out.rstrip().splitlines()[-1]
                self.assertIn("the wizard takes every default", last)  # the line before the wizard started

    def test_a_wizard_that_could_not_start_says_so_with_the_local_installer(self):
        """INSTALL-AC-49: once devkit is downloaded, "run it again" names its local install.sh, never the URL."""
        rc, out = self.m.pipe("--skip-packages", STUB_SETUP_RC="3")
        self.assertEqual(rc, 1, out)
        after = out.split("the wizard takes every default")[1]
        self.assertIn("The setup wizard could not start.", after)
        self.assertIn("~/Development/fireflysoftwaresolutions/devkit/install.sh --skip-packages", after)
        self.assertIn("~/.local/state/devkit/logs/", after)
        self.assertNotIn("raw.githubusercontent.com", out)

    def test_without_a_terminal_the_wizard_takes_every_default(self):
        rc, out = self.m.pipe("--skip-packages")
        self.assertEqual(rc, 0, out)
        (setup,) = self.m.setups()
        self.assertEqual(setup["argv"], ["setup", "--skip-packages", "--yes"])
        self.assertEqual((setup["stdin_tty"], setup["wizard_asks"]), (False, False))

    def test_no_command_gets_the_pipe_the_script_arrives_through(self):
        rc, out = self.m.pipe("--skip-packages")
        self.assertEqual(rc, 0, out)
        gh = [c for c in self.m.calls() if c.startswith("gh ")]
        self.assertTrue(gh)
        self.assertEqual([c for c in gh if c.endswith("(pipe)")], [], gh)  # its input is /dev/null, said plainly

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

    def test_a_failed_step_says_why_and_how_to_run_it_again(self):
        """I-6 before the download: the reason is git's own error line, and the way to run it again is the command
        the person started with (INSTALL-AC-49: never a URL they may not have used)."""
        rc, out = self.m.pipe("--yes", DEVKIT_REPO=f"file://{self.m.root}/missing")
        self.assertEqual(rc, 1, out)
        self.assertIn("✗ devkit did not download.", out)
        self.assertRegex(out, r"Reason: fatal: '.*missing' does not appear to be a git repository")
        self.assertIn("Fix: run the installer again (the same command you started with).", out)
        self.assertIn("If it fails the same way, send the full output file to your devkit contact.", out)
        self.assertIn("Running it again is safe: finished steps are not repeated.", out)
        self.assertIn("Full output: ~/.local/state/devkit/logs/install.log", out)
        self.assertEqual(out.count("Reason:"), 1)
        self.assertNotIn("raw.githubusercontent.com", out)
        self.assertNotIn("exit code", out)
        self.assertEqual(self.m.setups(), [])
        log = self.m.home / ".local" / "state" / "devkit" / "logs" / "install.log"
        self.assertIn("does not appear to be a git repository", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_a_failed_homebrew_install_shows_its_error_line_and_where_the_full_output_is(self):
        """I-6: Homebrew's output stays in the log until a step fails; then its error line is the reason."""
        (self.m.prefix / "bin" / "python3").unlink()
        brew = self.m.stubs / "brew"
        brew.write_text(brew.read_text().replace(
            'echo "brew $*" >> "$STUB_LOG"',
            'echo "brew $*" >> "$STUB_LOG"\n'
            'if [ "$1" = install ]; then echo "==> Fetching python"; '
            'echo "Error: python@3.15: the bottle needs the Xcode Command Line Tools" >&2; '
            'echo "==> Cleaning up"; exit 1; fi'))
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 1, out)
        self.assertIn("✗ Python did not install.", out)
        self.assertIn("Reason: Error: python@3.15: the bottle needs the Xcode Command Line Tools", out)
        self.assertNotIn("==> Fetching python", out)
        log = (self.m.home / ".local" / "state" / "devkit" / "logs" / "install.log").read_text()
        self.assertIn("==> Fetching python", log)
        self.assertFalse(self.m.devkit.exists())  # nothing after the failed step ran

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

    def test_homebrew_installs_without_asking_and_its_output_goes_to_the_log(self):
        (self.m.prefix / "bin" / "python3").unlink()  # Python is missing: the installer has Homebrew install it
        brew = self.m.stubs / "brew"
        brew.write_text(brew.read_text().replace(
            'echo "brew $*" >> "$STUB_LOG"',
            'echo "brew $* (ask: ${HOMEBREW_NO_ASK:+off})" >> "$STUB_LOG"\n'
            'if [ "$1" = install ]; then echo "==> Installing $2"; '
            '[ -n "${HOMEBREW_NO_ASK:-}" ] || { echo "Do you want to proceed with the installation? [y/n]"; '
            'read -r answer || exit 1; }; '
            'printf \'#!/bin/sh\\nexec "%s" "$@"\\n\' "' + sys.executable + '" > "$STUB_PREFIX/bin/python3"; '
            'chmod +x "$STUB_PREFIX/bin/python3"; fi'))
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 0, out)
        self.assertIn("brew install python (ask: off)", self.m.calls())
        self.assertNotIn("==> Installing python", out)  # R23: Homebrew's own output only when it fails
        self.assertNotIn("Do you want to proceed", out)
        self.assertRegex(out, r"✓ Python \d+\.\d+")
        log = self.m.home / ".local" / "state" / "devkit" / "logs" / "install.log"
        self.assertIn("==> Installing python", log.read_text())

    def test_an_annotated_release_tag_is_checked_out_without_a_warning(self):
        """INSTALL-AC-40: git's "is not a commit" warning for an annotated tag never reaches the screen."""
        for version in ("26.11.11", "26.11.12"):
            (self.m.remote / "VERSION").write_text(version + "\n")
            git(self.m.remote, "add", "-A")
            git(self.m.remote, "commit", "-q", "-m", version)
            git(self.m.remote, "tag", "-a", "v" + version, "-m", "v" + version)
        git(self.m.remote, "commit", "-q", "--allow-empty", "-m", "after the release")
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("is not a commit", out)
        self.assertNotIn("warning:", out)
        self.assertEqual(self.m.release(), "v26.11.12")
        head = git(self.m.devkit, "rev-parse", "HEAD")
        self.assertEqual(head, git(self.m.remote, "rev-parse", "v26.11.12^{commit}"))  # what `dev install` pins
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.m.devkit), "symbolic-ref", "-q", "HEAD"],
                                           env=GIT_ENV, capture_output=True).returncode, 0)  # detached at the tag
        self.assertIn("✓ Downloaded devkit 26.11.12 to ~/Development/fireflysoftwaresolutions/devkit", out)

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
    Apple silicon only: a new install needs Apple silicon, and an Intel Mac that already has Homebrew still runs.
    A terminal under Rosetta makes an Apple silicon Mac report x86_64: the installer tells it from an Intel Mac."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()
        (self.m.stubs / "curl").write_text(CURL)
        (self.m.stubs / "curl").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def nothing_changed(self):
        # only uname and sysctl ran: no curl, brew or gh, no checkout, no shell profile, no wizard
        self.assertEqual([c for c in self.m.calls() if not c.startswith(("uname ", "sysctl "))], [])
        self.assertFalse(self.m.devkit.exists())
        self.assertFalse((self.m.home / ".zprofile").exists())
        self.assertEqual(self.m.setups(), [])

    # brew is neither on PATH nor where Homebrew installs it: the installer says why in its own words, touches nothing
    def test_an_intel_mac_without_homebrew_stops_before_changing_anything(self):
        (self.m.stubs / "brew").unlink()
        rc, out = self.m.pipe(STUB_ARCH="x86_64")
        self.assertEqual(rc, 1, out)
        self.assertIn("devkit needs a Mac with Apple silicon for a new install, because Homebrew no longer installs "
                      "on Intel Macs. Nothing was changed.", out)
        self.assertIn("An Intel Mac that already has Homebrew can still run this installer.", out)
        self.assertNotIn("run the installer again", out)  # a retry would fail the same way
        self.assertNotIn("Rosetta", out)  # sysctl says this Mac has no Apple silicon: Rosetta is not the cause
        self.nothing_changed()

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

    # brew is only in /usr/local/bin, not on PATH: the first step and the Homebrew step find the same brew
    def test_an_intel_mac_with_homebrew_only_in_usr_local_continues_with_that_homebrew(self):
        usr_local = self.m.brew_paths[1]
        rc, out = self.m.pipe(STUB_ARCH="x86_64", **self.m.move_brew(usr_local))
        self.assertEqual(rc, 0, out)
        self.assertIn("macOS 26.1 on Intel", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.lstrip().startswith("!") and "Intel" in ln]), 1, out)
        self.assertIn(f"Homebrew 4.6.0 in {usr_local.parent.parent}", out)
        self.assertEqual(self.m.release(), "v26.11.10")
        self.assertEqual(len(self.m.setups()), 1)
        self.assertNotIn("curl", "\n".join(self.m.calls()))
        # /usr/local/bin is on the default PATH of macOS, so this Homebrew needs no line in ~/.zprofile. The stand-in is
        # not under /usr/local but in the temporary folder: the installer must know it as the Intel entry of its brew
        # paths, not by its folder, for this to hold here as on a real Intel Mac.
        zprofile = self.m.home / ".zprofile"
        self.assertNotIn("shellenv", zprofile.read_text() if zprofile.exists() else "")

    # Apple silicon under Rosetta reports x86_64: whether an x86 brew is on PATH or only the native one is installed,
    # the installer stops before anything changes and asks for a native terminal, never calling the Mac Intel
    def test_a_rosetta_terminal_on_apple_silicon_stops_and_asks_for_a_native_terminal(self):
        for brew in ("on PATH", "only in /opt/homebrew"):
            with self.subTest(brew=brew):
                extra = {} if brew == "on PATH" else self.m.move_brew(self.m.brew_paths[0])
                rc, out = self.m.pipe("--yes", STUB_ARCH="x86_64", STUB_ROSETTA="1", **extra)
                self.assertEqual(rc, 1, out)
                self.assertIn("This terminal runs under Rosetta, so this Mac with Apple silicon reports an x86_64 "
                              "processor. Nothing was changed.", out)
                # A new window of a Terminal that still runs stays under Rosetta: quit it, clear the box, open it again
                self.assertIn("1. Quit Terminal (Command-Q).", out)
                self.assertIn('clear "Open using Rosetta"', out)
                self.assertNotIn("new Terminal window", out)
                again = out.split("3. Open Terminal again and run the same command you started with.")[1]
                self.assertNotIn("raw.githubusercontent.com", out)  # INSTALL-AC-49: maybe not the command they used
                self.assertIn("In a shell started with arch -x86_64, type exit instead", again)
                # anything else, such as iTerm2: the Troubleshooting entry, at a heading that exists
                (anchor,) = re.findall(r"/docs/troubleshooting\.md#([\w-]+)", again)
                self.assertIn(anchor, heading_anchors(TROUBLESHOOTING))
                self.assertNotIn("Intel", out)
                self.nothing_changed()

    # sysctl is in /usr/sbin, which a PATH may leave out, as this one does: the installer still asks macOS's own sysctl,
    # instead of taking the Mac for an Intel one
    @unittest.skipUnless(apple_silicon(), "asks this computer's /usr/sbin/sysctl, which says 1 on Apple silicon only")
    def test_a_rosetta_terminal_is_told_from_intel_when_sysctl_is_not_on_path(self):
        (self.m.stubs / "sysctl").unlink()
        self.assertIsNone(shutil.which("sysctl", path=self.m.env()["PATH"]))
        rc, out = self.m.pipe(STUB_ARCH="x86_64")
        self.assertEqual(rc, 1, out)
        self.assertIn("This terminal runs under Rosetta", out)
        self.assertNotIn("Intel", out)
        self.nothing_changed()


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


ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WELCOME = "Press Enter to start, or Ctrl+C to stop."
RETRY = "Press Enter to try again, or type q and press Enter to quit."


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "needs a POSIX pseudo-terminal")
class TerminalTests(unittest.TestCase):
    """install.sh through a pipe in a terminal of its own, as `curl … | bash` runs in Terminal: the person types a key
    when the screen shows a given text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def terminal(self, *args, keys=(), **env):
        """-> (exit code, what the screen showed, without colors). `keys`: [(text, bytes)], each typed once the
        screen shows its text (after the previous key's); bytes may be a function that returns them, called at that
        moment, to look at the Mac then."""
        import pty
        master, slave = pty.openpty()
        proc = subprocess.Popen(["/bin/bash", "-s", "--", *args], stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                env=self.m.env(**env), cwd=str(self.m.root), start_new_session=True,
                                preexec_fn=_take_terminal)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and stop_session(proc))
        proc.stdin.write(INSTALL.read_bytes())
        proc.stdin.close()
        keys, out, seen, deadline = list(keys), b"", 0, time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = select.select([master], [], [], 0.2)[0]
            try:
                chunk = os.read(master, 65536) if ready else b""
            except OSError:  # the terminal closed: every process that had it has exited
                break
            out += chunk
            screen = ESCAPES.sub("", out.decode("utf-8", "replace"))
            if keys and keys[0][0] in screen[seen:]:
                text, typed = keys.pop(0)
                seen = screen.index(text, seen) + len(text)
                os.write(master, typed() if callable(typed) else typed)
            if not chunk and proc.poll() is not None:
                break
        os.close(master)
        rc = proc.wait(10)
        return rc, ESCAPES.sub("", out.decode("utf-8", "replace").replace("\r\n", "\n"))

    def test_through_a_pipe_in_a_terminal_sign_in_and_the_wizard_ask_there(self):
        rc, out = self.terminal(keys=[(WELCOME, b"\r")])
        self.assertEqual(rc, 0, out)
        self.assertIn("gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist --clipboard (terminal)",
                      self.m.calls())
        (setup,) = self.m.setups()
        self.assertEqual((setup["argv"], setup["stdin_tty"], setup["wizard_asks"]), (["setup"], True, True))
        # the terminal by its own device: macOS's kqueue refuses /dev/tty, and Claude Code's sign-in crashed on it
        if sys.platform == "darwin":
            self.assertEqual(setup["stdin_kqueue"], "ok")
        self.assertIn("5 of 5  Your setup\n  Opening the setup wizard. Press Enter to keep a recommended answer.", out)

    def test_the_welcome_waits_for_enter_before_anything_changes(self):
        """INSTALL-AC-10: the welcome says what will happen, then waits; nothing ran before Enter."""
        looked = {}

        def at_welcome():
            looked.update(calls=self.m.calls(), devkit=self.m.devkit.exists(), state=(self.m.home / ".local").exists())
            return b"\r"
        self.m.signed_in.touch()
        rc, out = self.terminal("--skip-packages", keys=[(WELCOME, at_welcome)])
        self.assertEqual(rc, 0, out)
        self.assertEqual(looked, {"calls": [], "devkit": False, "state": False})
        welcome = out.split(WELCOME)[0]
        for line in ("devkit installer", "This sets up devkit on this Mac. It takes about 15 to 30 minutes, most of it",
                     "  1  Check this Mac", "  2  Install Homebrew, git, Python and the GitHub CLI, if they are missing",
                     "  3  Download devkit to ~/Development/fireflysoftwaresolutions/devkit",
                     "  4  Sign you in to GitHub, in your browser",
                     "  5  Ask which agents and tools you use, then install and set them up", "Nothing has changed yet."):
            self.assertIn(line, welcome)
        self.assertNotIn("Your Mac password", welcome)  # Homebrew is here already: no password to ask for it
        self.assertLess(out.index(WELCOME), out.index("1 of 5  This Mac"))
        self.assertEqual(len(self.m.setups()), 1)

    def test_ctrl_c_at_the_welcome_changes_nothing(self):
        """INSTALL-AC-10: Ctrl+C at the welcome ends the installer with nothing installed, downloaded or written."""
        (self.m.stubs / "brew").unlink()  # a Mac without Homebrew: its installer will ask for the password
        rc, out = self.terminal(keys=[(WELCOME, b"\x03")])
        self.assertEqual(rc, 1, out)
        self.assertIn("Your Mac password may be asked once, to install Homebrew.", out)
        self.assertIn("Stopped. Nothing has changed.", out)
        self.assertEqual(self.m.calls(), [])
        self.assertFalse(self.m.devkit.exists())
        self.assertFalse((self.m.home / ".local").exists())

    def test_a_rerun_welcome_says_what_is_done_already(self):
        git(self.m.root, "clone", "-q", "--branch", "v26.11.10", str(self.m.remote), str(self.m.devkit))
        env = self.m.move_brew(self.m.brew_paths[0])  # Homebrew where it installs, with its Python
        rc, out = self.terminal(keys=[(WELCOME, b"\x03")], **env)
        self.assertEqual(rc, 1, out)
        self.assertIn("  2  Install Homebrew, git, Python and the GitHub CLI (already done)", out)
        self.assertIn("  3  Download devkit to ~/Development/fireflysoftwaresolutions/devkit (already done)", out)
        self.assertIn("This takes a minute, then continues your setup.", out)
        self.assertNotIn("15 to 30 minutes", out)
        self.assertNotIn("(already done)", out.split("  2  ")[0])  # checking this Mac is never skipped

    def test_yes_in_a_terminal_starts_without_the_welcome(self):
        """INSTALL-AC-11, R5: --yes is the consent."""
        self.m.signed_in.touch()
        rc, out = self.terminal("--yes", "--skip-packages")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(WELCOME, out)
        self.assertIn("1 of 5  This Mac", out)

    def test_a_failed_github_sign_in_can_be_tried_again(self):
        """INSTALL-AC-41: the sign-in says why it did not finish, and Enter starts it again."""
        rc, out = self.terminal(keys=[(WELCOME, b"\r"), (RETRY, b"\r")], STUB_GH_LOGIN="fail-once")
        self.assertEqual(rc, 0, out)
        for line in ("Your browser opens GitHub. There:",
                     "1. Paste the one-time code shown below (it is copied for you).",
                     "Then come back here. This waits up to 10 minutes; Ctrl+C stops it.",
                     "✗ You are not signed in to GitHub.", "Reason: GitHub said: error: device code expired",
                     "devkit needs GitHub for your project notes, so it cannot go on without it.", RETRY,
                     "✓ Signed in to GitHub as ada"):
            self.assertIn(line, out)
        self.assertEqual(len([c for c in self.m.calls() if c.startswith("gh auth login -h")]), 2)
        self.assertEqual(len(self.m.setups()), 1)

    def test_q_after_a_failed_github_sign_in_quits_before_the_wizard(self):
        """INSTALL-AC-41: q ends the installer with exit code 2 and the local installer to continue with."""
        rc, out = self.terminal(keys=[(WELCOME, b"\r"), (RETRY, b"q\r")], STUB_GH_LOGIN="fail-once")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.m.setups(), [])
        self.assertIn("Stopped before setup. devkit is downloaded; nothing else is set up yet.", out)
        self.assertIn("To continue, run:  ~/Development/fireflysoftwaresolutions/devkit/install.sh", out)
        self.assertNotIn("raw.githubusercontent.com", out)

    def test_ctrl_c_stops_only_the_github_sign_in(self):
        """R24: Ctrl+C in the browser sign-in brings back the choice, with the reason; the installer goes on."""
        rc, out = self.terminal(keys=[(WELCOME, b"\r"), ("one-time code: 4F2A-9C1D", b"\x03"), (RETRY, b"q\r")],
                                STUB_GH_LOGIN="hang")
        self.assertEqual(rc, 2, out)
        self.assertIn("Reason: you stopped the sign-in.", out)
        self.assertNotIn("Stopped during", out)

    def test_a_github_sign_in_past_its_time_limit_is_stopped(self):
        """R24: a browser page nobody finishes is stopped at the time limit (one second here)."""
        rc, out = self.terminal(keys=[(WELCOME, b"\r"), (RETRY, b"q\r")], STUB_GH_LOGIN="hang",
                                DEVKIT_TEST_GH_LIMIT="1")
        self.assertEqual(rc, 2, out)
        self.assertIn("Reason: the browser page was not finished within 10 minutes.", out)

    def test_ctrl_c_during_a_step_says_how_to_continue(self):
        """I-6: Ctrl+C stops the installer during a step, says nothing is broken and how to continue."""
        (self.m.prefix / "bin" / "python3").unlink()
        brew = self.m.stubs / "brew"
        brew.write_text(brew.read_text().replace('echo "brew $*" >> "$STUB_LOG"',
                                                 'echo "brew $*" >> "$STUB_LOG"\n'
                                                 'if [ "$1" = install ]; then exec sleep 30; fi'))
        rc, out = self.terminal(keys=[(WELCOME, b"\r"), ("Installing Python", b"\x03")])
        self.assertEqual(rc, 130, out)
        self.assertIn('! Stopped during "Homebrew, git, Python and the GitHub CLI".', out)
        self.assertIn("Nothing is broken.", out)
        self.assertIn("To continue, run the same command again. Finished steps are not repeated.", out)
        self.assertFalse(self.m.devkit.exists())


if __name__ == "__main__":
    unittest.main()
