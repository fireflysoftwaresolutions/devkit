# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A fresh install end to end, as a new Mac meets it: install.sh clones devkit at its release tag, and the setup
wizard runs `bin/dev install` as a child process with its output captured and no terminal.

The fixture is built from files: this working tree's devkit (without tests and build output) is committed into a
temporary repository with an annotated tag v<VERSION>, which the fixture's own install.sh downloads. `bin/dev install`
runs in a temporary HOME inside a macOS sandbox (sandbox-exec) that refuses to start Homebrew's tools, the agents,
launchctl and open, refuses writes to the real home folder, and refuses network connections: devkit adds Homebrew's
folders to PATH itself, so stubs on PATH alone could not keep it away from this Mac's Homebrew.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_install import Machine  # noqa: E402  (install.sh with stand-ins for Homebrew and the GitHub CLI)

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
TAG = "v" + VERSION
SANDBOX = "/usr/bin/sandbox-exec"
ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", **ISOLATED)
# What the fixture leaves out: git's own folder, the tests, caches and build output.
SKIP = shutil.ignore_patterns(".git", "tests", "__pycache__", "*.pyc", ".ruff_cache", "dist", "node_modules",
                              ".DS_Store", ".venv")


def git(cwd, *args, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env or GIT_ENV)


def sandbox_profile(real_home: str, allow: list) -> str:
    """Everything is allowed but starting programs from Homebrew, /usr/local or the real home folder (the Python that
    runs devkit excepted), launchctl, open, osascript and security; writing to the real home folder; and network
    connections to other computers."""
    def lit(p):
        return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'
    allowed = " ".join(f"(subpath {lit(p)})" if Path(p).is_dir() else f"(literal {lit(p)})" for p in allow)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        f'(deny process-exec (regex #"^/opt/homebrew/") (regex #"^/usr/local/") (subpath {lit(real_home)})',
        '  (literal "/bin/launchctl") (literal "/usr/bin/open") (literal "/usr/bin/osascript")',
        '  (literal "/usr/bin/security"))',
        f"(allow process-exec {allowed})",
        f"(deny file-write* (subpath {lit(real_home)}))",
        "(deny network-outbound (remote ip))",
    ]) + "\n"


@unittest.skipUnless(sys.platform == "darwin" and os.access(SANDBOX, os.X_OK), "needs macOS's sandbox-exec")
class FreshInstallTests(unittest.TestCase):
    """`bin/dev install` on a Mac that has never had devkit, from a clone at the release tag."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(cls.tmp.name))
        cls.root = root
        src = root / "origin"
        shutil.copytree(str(ROOT), str(src), ignore=SKIP, symlinks=True)
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", VERSION),
                     ("tag", "-a", TAG, "-m", TAG)):
            r = git(src, *args)
            assert r.returncode == 0, r.stderr
        cls.origin = src

    @classmethod
    def tearDownClass(cls):
        for d, _, _ in os.walk(cls.root):  # pinned copies are read-only
            # the test sets the mode of its own temporary folders, so that they can be deleted
            os.chmod(d, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        cls.tmp.cleanup()

    def setUp(self):
        self.m = Machine(Path(tempfile.mkdtemp(dir=str(self.root), prefix="mac-")))
        self.home, self.devkit = self.m.home, self.m.devkit

    def clone(self) -> str:
        """install.sh's download, through a pipe as `curl … | bash` runs it, with stand-ins for Homebrew and the
        GitHub CLI (test_install.Machine): the newest release of the fixture, its annotated tag checked out."""
        rc, out = self.m.pipe("--no-setup", DEVKIT_REPO=f"file://{self.origin}")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"✓ Downloaded devkit {VERSION} to ~/Development/fireflysoftwaresolutions/devkit", out)
        self.assertEqual(git(self.devkit, "describe", "--tags", "--exact-match").stdout.strip(), TAG)
        self.assertNotIn("is not a commit", out)  # git's warning for `clone --branch <annotated tag>`
        return out

    def env(self) -> dict:
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
               "TERM": "xterm-256color", "USER": os.environ.get("USER", "ada"), "TMPDIR": str(self.root),
               "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "HOMEBREW_NO_AUTO_UPDATE": "1",
               "GH_PROMPT_DISABLED": "1"}
        return env

    def sandboxed(self, *argv) -> list:
        """argv run in the sandbox; the Python that runs these tests may start."""
        real_home = os.path.realpath(os.path.expanduser("~"))
        py = [sys.executable, os.path.realpath(sys.executable), os.path.realpath(sys.base_prefix)]
        profile = self.root / "sandbox.sb"
        profile.write_text(sandbox_profile(real_home, py))
        return [SANDBOX, "-f", str(profile), *argv]

    def install(self, *args, timeout=600) -> subprocess.CompletedProcess:
        """`bin/dev install` the way the wizard's Configure step runs it: the Python that runs the wizard, -I -S as
        the shebang, no terminal, stdout and stderr captured (here apart, to tell them apart)."""
        argv = self.sandboxed(sys.executable, "-I", "-S", str(self.devkit / "bin" / "dev"), "install", *args)
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=self.env(),
                              cwd=str(self.home), timeout=timeout)

    def test_a_fresh_install_from_the_release_clone_pins_the_copy_and_runs_its_steps(self):
        self.clone()
        r = self.install()
        out = f"exit {r.returncode}\n--- stdout\n{r.stdout[-6000:]}\n--- stderr\n{r.stderr[-6000:]}"
        self.assertNotIn("devkit bug", r.stderr, out)
        self.assertEqual(r.returncode, 0, out)
        rec = json.loads((self.home / ".local" / "share" / "devkit" / "install.json").read_text())
        self.assertEqual((rec["version"], Path(rec["path"]).parent),
                         (VERSION, self.home / ".local" / "share" / "devkit" / "releases"), out)
        self.assertIn("running from ~/.local/share/devkit/releases/", r.stdout, out)  # the second half ran
        self.assertIn("▸ Pinned copies", r.stdout, out)  # and every step after it

    def test_each_half_prints_its_lines_as_it_goes_so_a_killed_install_keeps_them(self):
        """`dev install` runs in two processes: the checkout pins the copy, then os.execv starts the pinned copy for
        the steps. A process killed by a signal never flushes what it buffered, and the wizard then showed only the
        first half's banner. So each step header must reach the reader before the step's work: when the first
        "Pinned copy" header arrives, nothing is pinned yet; when the second one arrives, the pinned copy has not
        linked ~/.local/bin/dev yet. The reader kills the install there."""
        self.clone()
        argv = self.sandboxed(sys.executable, "-I", "-S", str(self.devkit / "bin" / "dev"), "install")
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=self.env(), cwd=str(self.home), start_new_session=True)
        record, link = self.home / ".local" / "share" / "devkit" / "install.json", self.home / ".local" / "bin" / "dev"
        seen, at_header = [], []
        try:
            for raw in p.stdout:
                seen.append(raw.decode("utf-8", "replace").rstrip("\n"))
                if seen[-1].startswith("▸ Pinned copy of devkit"):
                    at_header.append((record.exists(), os.path.lexists(link)))
                    if len(at_header) == 2:
                        os.killpg(p.pid, signal.SIGKILL)
                        break
        finally:
            p.stdout.close()
            rc = p.wait(60)
        self.assertEqual(rc, -signal.SIGKILL, "\n".join(seen))
        self.assertEqual(at_header, [(False, False), (True, False)], "\n".join(seen))


if __name__ == "__main__":
    unittest.main()
