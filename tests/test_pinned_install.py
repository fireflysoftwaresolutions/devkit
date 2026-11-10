# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The pinned install (spec C10, security finding K2): `dev install` pins a read-only copy of one devkit commit with a
manifest of checksums, and the hooks, ~/.local/bin/dev and the status line run that copy, never the checkout.

Everything runs in a sandbox: a temporary HOME (core.HOME, STATE, CLAUDE_HOME, CODEX_HOME, the audit and the managed
settings file moved into it), a temporary devkit-like repository whose origin is a local repository with a release
tag, and a fake Codex app-server. No network, no real `dev install`, nothing in ~/.claude, ~/.codex or ~/.local.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import approvals, audit, cli, codexrpc, core, gitx, guard, hooks, policy, progress  # noqa: E402
from devkit import registry, setup  # noqa: E402
from test_install import Machine  # noqa: E402  (install.sh against stub brew, gh and a local repository)
import _sandbox  # noqa: E402  (stand-ins first: no child reaches this Mac's codex, claude, brew or gh)

ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", **ISOLATED)
DEV_STUB = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def commit(repo: Path, version: str, tag: bool = False, **files):
    """Commit VERSION and `files` ({"lib/devkit/guard.py": text}); tag v<version> (annotated) when asked."""
    (repo / "VERSION").write_text(version + "\n")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", version)
    if tag:
        git(repo, "tag", "-a", "v" + version, "-m", "v" + version)


class Execd(Exception):
    """os.execv was reached: the install would continue in another process."""


class FakeCodexServer(codexrpc.CodexRPC):
    """codex app-server as the installer uses it. hooks/list hashes each command of hooks.json, and a hook counts as
    trusted only while its trusted hash is its current hash, as Codex does. trust_hooks and hooks are devkit's own."""
    trusted = {}
    hooks_file = None

    def __init__(self, timeout=30):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, params=None, timeout=None):
        assert method == "hooks/list", method
        hooks = []
        for event, groups in json.loads(Path(self.hooks_file).read_text())["hooks"].items():
            for i, group in enumerate(groups):
                for j, h in enumerate(group["hooks"]):
                    key, cur = f"{event}:{i}:{j}", hashlib.sha256(h["command"].encode()).hexdigest()
                    want = FakeCodexServer.trusted.get(key)
                    hooks.append({"key": key, "eventName": event, "sourcePath": str(self.hooks_file),
                                  "handlerType": h["type"], "command": h["command"], "async": h.get("async", False),
                                  "matcher": group.get("matcher"), "timeoutSec": h.get("timeout"),
                                  "statusMessage": h.get("statusMessage"),
                                  "currentHash": cur, "trustStatus": "untrusted" if want is None else
                                  "trusted" if want == cur else "modified"})
        return {"data": [{"hooks": hooks}]}

    def write(self, edits, reload=True):
        for key, value, _ in edits:
            if key == "hooks.state":
                FakeCodexServer.trusted.update({k: v["trusted_hash"] for k, v in value.items()})


class Sandbox(unittest.TestCase):
    """A temporary HOME holding every path devkit installs to, and `self.src`: a devkit-like checkout whose origin has
    the release v26.11.3 on its HEAD."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.root, self.home = root, root / "home"
        self.home.mkdir()
        self.state = self.home / ".local" / "state" / "devkit"
        for p in (mock.patch.object(core, "HOME", self.home), mock.patch.object(setup, "HOME", self.home),
                  mock.patch.object(core, "STATE", self.state), mock.patch.object(setup, "STATE", self.state),
                  mock.patch.object(setup, "CLAUDE_HOME", self.home / ".claude"),
                  mock.patch.object(setup, "CODEX_HOME", self.home / ".codex"),
                  mock.patch.object(setup, "ZSHRC", self.home / ".zshrc"),
                  mock.patch.object(setup, "backup", lambda *a, **k: None),
                  mock.patch.object(setup, "_QUIET", [True]),
                  mock.patch.object(audit, "AUDIT", self.state / "audit"),
                  mock.patch.object(policy, "MANAGED_FILE", self.home / "managed-settings.json"),
                  mock.patch.dict(core.CFG["claude"], {"default_mode": "bypassPermissions", "statusline": True}),
                  mock.patch.dict(core.GIT_ENV, ISOLATED)):
            p.start()
            self.addCleanup(p.stop)
        self.origin, self.src = root / "origin", root / "devkit"
        (self.origin / "bin").mkdir(parents=True)
        (self.origin / "bin" / "dev").write_text(DEV_STUB)
        (self.origin / "bin" / "dev").chmod(0o755)
        git(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, "26.11.3", tag=True, **{"lib/devkit/__init__.py": "", "lib/devkit/guard.py": "DENY = True\n",
                                                    "config/git/ignore": ".DS_Store\n", "README.md": "devkit\n"})
        git(root, "clone", "-q", str(self.origin), str(self.src))

    def tearDown(self):
        for d, _, _ in os.walk(self.root):  # pinned copies are read-only
            # the test sets the file mode it checks, on its own temporary files
            os.chmod(d, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        self.tmp.cleanup()

    def head(self, repo=None) -> str:
        return git(repo or self.src, "rev-parse", "HEAD")

    def install(self, source=None, why=None) -> Path:
        """What a full `dev install` records before it continues in the copy: pin, then install.json."""
        info = setup.pin(source or self.src, unverified_why=why)
        setup.record_install(info)
        return Path(info["path"])

    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def audits(self) -> list:
        return [json.loads(ln) for f in sorted((self.state / "audit").glob("*.jsonl")) for ln in f.read_text().splitlines()]

    def checks(self) -> dict:
        return {name: (ok, detail, fix) for name, ok, detail, fix in setup.pin_checks()}


class PinTests(Sandbox):
    def test_a_release_is_pinned_read_only_with_a_checksum_per_file(self):
        info = setup.pin(self.src)
        dest = Path(info["path"])
        self.assertEqual(dest, core.pin_root() / f"26.11.3-{self.head()[:7]}")
        self.assertEqual(info["verified"]["how"], "release")  # v26.11.3 is on origin, on this commit
        man = json.loads((dest / core.PIN_MANIFEST).read_text())
        self.assertEqual((man["commit"], man["version"]), (self.head(), "26.11.3"))
        for rel in git(self.src, "ls-files").splitlines():
            self.assertEqual((dest / rel).read_bytes(), (self.src / rel).read_bytes(), rel)
            self.assertEqual(man["files"][rel], sha256(self.src / rel), rel)
        if not getattr(sys, "pycache_prefix", None):  # Apple's /usr/bin/python3 caches in ~/Library/Caches instead
            pyc = Path(importlib.util.cache_from_source(str(dest / "lib" / "devkit" / "guard.py")))
            self.assertTrue(pyc.exists())  # compiled before it became read-only: hooks do not recompile it each time
            self.assertEqual(man["files"][pyc.relative_to(dest).as_posix()], sha256(pyc))
        mode = lambda rel: stat.S_IMODE((dest / rel).stat().st_mode)  # noqa: E731
        self.assertEqual((mode("README.md"), mode("bin/dev"), mode("."), mode("lib/devkit")),
                         (0o444, 0o555, 0o555, 0o555))  # bin/dev keeps its x bit: the hooks run it
        with self.assertRaises(PermissionError):
            (dest / "lib" / "devkit" / "guard.py").write_text("DENY = False\n")
        self.assertEqual(setup.verify_pin(dest), [])

    def test_a_checkout_with_uncommitted_changes_to_tracked_files_is_refused(self):
        (self.src / "lib" / "devkit" / "guard.py").write_text("DENY = False\n")
        with self.assertRaises(setup.PinError) as e:
            setup.pin(self.src)
        self.assertIn("uncommitted changes to 1 tracked file(s) (lib/devkit/guard.py)", str(e.exception))
        self.assertFalse(core.pin_root().exists() and any(core.pin_root().iterdir()))
        git(self.src, "checkout", "--", "lib/devkit/guard.py")
        (self.src / "notes.txt").write_text("mine")  # untracked: not part of HEAD, so it neither blocks nor is copied
        dest = Path(setup.pin(self.src)["path"])
        self.assertFalse((dest / "notes.txt").exists())

    def test_an_untested_head_is_refused_without_unverified_and_a_reason(self):
        commit(self.src, "26.11.4", **{"lib/devkit/guard.py": "DENY = 'maybe'\n"})
        with self.assertRaises(setup.PinError) as e:
            setup.pin(self.src)
        self.assertIn("is not verified", str(e.exception))
        self.assertIn('dev install --unverified --why "<reason>"', str(e.exception))
        git(self.src, "tag", "v26.11.4")  # a tag only this checkout has (anyone, an agent too, can make one)
        with self.assertRaises(setup.PinError) as e:
            setup.pin(self.src)
        self.assertIn("origin has no tag v26.11.4", str(e.exception))
        tree = git(self.src, "rev-parse", "HEAD^{tree}")
        gitx.record_evidence(gitx.ctx(self.src), "attested", tree)  # `--ci-passed`: a claim, not a run
        with self.assertRaises(setup.PinError):
            setup.pin(self.src)
        self.assertFalse(core.pin_root().exists() and any(core.pin_root().iterdir()))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_install(Namespace(unverified=True, why=" ", only=None, version=None, as_pinned=False))
        self.assertEqual(rc, 2)
        info = setup.pin(self.src, unverified_why="trying the fix for K2 before the release")
        self.assertEqual(info["verified"]["how"], "unverified")
        rec = [r for r in self.audits() if r["action"] == "install.pin"][-1]
        self.assertEqual((rec["outcome"], rec["ref"]["result"]), ("recorded", "unverified"))  # audit schema v2
        self.assertIn("trying the fix for K2 before the release", rec["detail"])

    def test_a_head_whose_test_gate_passed_is_pinned(self):
        commit(self.src, "26.11.4", **{"lib/devkit/guard.py": "DENY = True  # tested\n"})
        gitx.record_evidence(gitx.ctx(self.src), "run", git(self.src, "rev-parse", "HEAD^{tree}"))  # dev ci --run
        info = setup.pin(self.src)
        self.assertEqual((info["verified"]["how"], info["version"]), ("tests", "26.11.4"))

    def test_a_copy_that_does_not_compile_is_refused_even_unverified(self):
        commit(self.src, "26.11.4", **{"lib/devkit/guard.py": "def deny(:\n"})
        with self.assertRaises(setup.PinError) as e:
            setup.pin(self.src, unverified_why="in a hurry")
        self.assertIn("lib/devkit/guard.py does not compile", str(e.exception))
        self.assertEqual(sorted(core.pin_root().iterdir()), [])  # the half-built copy is gone too


class HookTests(Sandbox):
    def test_hooks_the_status_line_and_dev_run_the_pinned_copy(self):
        pin = self.install()
        setup.write_claude_settings(include_hooks=True)
        s = self.settings()
        commands = [h["command"] for groups in s["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertGreaterEqual(len(commands), 6)
        for c in commands + [s["statusLine"]["command"]]:
            self.assertTrue(c.startswith(f"'{pin}/bin/dev' hook "), c)
        self.assertNotIn(str(core.DEVKIT_HOME), json.dumps(s))  # never the checkout this code runs from
        setup.install_cli()
        self.assertEqual(os.path.realpath(self.home / ".local" / "bin" / "dev"), os.path.realpath(pin / "bin" / "dev"))
        self.assertIn(f'source "{pin}/shell/devkit.zsh"', (self.home / ".zshrc").read_text())

    def test_codex_hooks_move_to_the_new_copy_and_are_trusted_again(self):
        hooks_f = self.home / ".codex" / "hooks.json"
        hooks_f.parent.mkdir()
        FakeCodexServer.trusted, FakeCodexServer.hooks_file = {}, hooks_f
        with mock.patch.object(codexrpc, "CodexRPC", FakeCodexServer), \
                mock.patch.object(registry, "discover", lambda: ([], [], [])):
            self.install()
            setup.write_codex_config(quiet=True)
            first = dict(FakeCodexServer.trusted)
            commit(self.origin, "26.11.4", tag=True)  # the next release, fetched as `dev update` does
            git(self.src, "fetch", "-q", "--tags")
            git(self.src, "-c", "advice.detachedHead=false", "checkout", "-q", "v26.11.4")
            pin = self.install()
            setup.write_codex_config(quiet=True)
        commands = [h["command"] for gs in json.loads(hooks_f.read_text())["hooks"].values() for g in gs
                    for h in g["hooks"]]
        self.assertTrue(commands)
        self.assertTrue(all(c.startswith(f"'{pin}/bin/dev' hook ") for c in commands), commands)
        statuses = {h["trustStatus"] for h in FakeCodexServer().hooks(self.home)}
        self.assertEqual(statuses, {"trusted"})  # the changed commands were trusted again, not left "modified"
        self.assertEqual(set(first), set(FakeCodexServer.trusted))
        self.assertTrue(all(first[k] != FakeCodexServer.trusted[k] for k in first))

    def test_install_from_the_checkout_continues_in_the_pinned_copy(self):
        out = io.StringIO()
        with mock.patch.object(core, "source_home", lambda: self.src), \
                mock.patch("os.execv", side_effect=Execd) as execv, contextlib.redirect_stdout(out):
            with self.assertRaises(Execd):
                setup.run_install()
            pin = core.install_record()["path"]
            self.assertEqual(execv.call_args[0][1][1:], ["-I", "-S", f"{pin}/bin/dev", "install", "--as-pinned"])
            commit(self.src, "26.11.4")  # untested: `--only` re-applies from the installed copy and pins nothing
            with self.assertRaises(Execd):
                setup.run_install(only={"claude"})
            self.assertEqual(execv.call_args[0][1][1:],
                             ["-I", "-S", f"{pin}/bin/dev", "install", "--as-pinned", "--only", "claude"])
            self.assertEqual(core.install_record()["path"], pin)
            (self.src / "README.md").write_text("changed\n")  # a dirty checkout: refused, nothing runs
            execv.reset_mock()
            self.assertEqual(setup.run_install(), 1)
            execv.assert_not_called()
        self.assertIn("uncommitted changes", out.getvalue())


class PruneTests(Sandbox):
    def test_old_copies_are_pruned_but_never_one_in_use(self):
        names = []
        for i in range(6):
            commit(self.src, f"26.11.{4 + i}")
            names.append(Path(setup.pin(self.src, unverified_why="prune test")["path"]).name)
        setup.record_install(setup.select_pin("26.11.4"))  # rolled back to the oldest: it is in use
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": f"'{core.pin_root() / names[1]}/bin/dev' hook guard --agent claude"}]}]}}))
        leftover = core.pin_root() / f".26.11.99-abcdef0.tmp-{os.getpid()}"  # an install that stopped half-way
        (leftover / "lib").mkdir(parents=True)
        os.chmod(leftover / "lib", 0o555)
        os.utime(leftover, (1_000, 1_000))
        removed = setup.prune_pins()
        self.assertEqual(removed, [names[2]])  # beyond the newest three and used by nothing
        self.assertEqual(sorted(d.name for d in core.pin_root().iterdir()), sorted(names[:2] + names[3:]))


class DoctorTests(Sandbox):
    def test_doctor_fails_on_a_tampered_file_and_install_rebuilds_the_copy(self):
        pin = self.install()
        setup.write_claude_settings(include_hooks=True)
        setup.install_cli()
        self.assertTrue(all(ok for ok, _, _ in self.checks().values()), self.checks())
        guard = pin / "lib" / "devkit" / "guard.py"
        os.chmod(guard, 0o644)
        guard.write_text("DENY = False\n")
        os.chmod(guard, 0o444)
        ok, detail, fix = self.checks()["pinned copy matches its manifest"]
        self.assertFalse(ok)
        self.assertIn("changed lib/devkit/guard.py", detail)
        self.assertTrue(fix.startswith("dev install"), fix)
        man_f = pin / core.PIN_MANIFEST  # a manifest rewritten to match the change is noticed too
        man = json.loads(man_f.read_text())
        man["files"]["lib/devkit/guard.py"] = sha256(guard)
        # the test sets the file mode it checks, on its own temporary files
        os.chmod(pin, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(man_f, 0o644)
        man_f.write_text(json.dumps(man))
        os.chmod(man_f, 0o444)
        os.chmod(pin, 0o555)
        ok, detail, _ = self.checks()["pinned copy matches its manifest"]
        self.assertFalse(ok)
        self.assertIn(f"changed {core.PIN_MANIFEST}", detail)
        setup.record_install(setup.pin(self.src))  # the fix: `dev install` rebuilds the copy from git
        self.assertEqual(guard.read_text(), "DENY = True\n")
        self.assertTrue(all(ok for ok, _, _ in self.checks().values()), self.checks())
        self.assertIn("tampered", [r.get("ref", {}).get("result") for r in self.audits() if r["action"] == "install.pin"])

    def test_doctor_fails_when_a_hook_runs_the_checkout_or_nothing_is_pinned(self):
        self.assertEqual(self.checks(), {"hooks run a pinned copy": (
            False, "not pinned: the hooks run whatever the settings name", "dev install")})
        self.install()
        setup.write_claude_settings(include_hooks=True)
        s = self.settings()
        # the hook runs the checkout the copy was pinned from (the sandbox's, so the check does not depend on where
        # the tree under test sits: an exported copy's folder need not be named devkit)
        s["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = f"'{self.src}/bin/dev' hook guard --agent claude"
        (self.home / ".claude" / "settings.json").write_text(json.dumps(s))
        ok, detail, _ = self.checks()["hooks run the pinned copy"]
        self.assertFalse(ok)
        self.assertIn(f"Claude PreToolUse runs {core.tilde(self.src)} (the checkout)", detail)


class RealTreeTests(Sandbox):
    """devkit itself, as this working tree has it, committed in a new repository and pinned."""

    def pin_devkit(self) -> tuple:
        clone = self.root / "checkout"
        shutil.copytree(ROOT, clone, ignore=shutil.ignore_patterns(".git", "__pycache__", "dist", ".DS_Store",
                                                                    "node_modules", ".venv"))
        git(clone, "init", "-q", "-b", "main")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "devkit")
        return clone, self.install(clone, why="pinned-install test")

    def test_a_broken_guard_in_the_checkout_leaves_the_pinned_guard_denying(self):
        """The acceptance test of K2: a syntax error in the checkout's guard.py, and the hooks still deny."""
        clone, _ = self.pin_devkit()
        command = next(h["command"] for g in setup.load_conf("claude/settings.json")["hooks"]["PreToolUse"]
                       for h in g["hooks"] if " hook guard " in h["command"])
        (clone / "lib" / "devkit" / "guard.py").write_text("def deny(:\n")  # a member's half-done edit
        payload = json.dumps({"session_id": "k2", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "gh pr merge 12"}, "cwd": str(self.home)})
        env = _sandbox.env({"HOME": str(self.home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                            "LANG": "en_US.UTF-8", **ISOLATED})
        run = lambda argv: subprocess.run(argv, input=payload, capture_output=True, text=True, env=env,  # noqa: E731
                                          cwd=str(self.home), timeout=120)
        pinned = run(["/bin/sh", "-c", command])
        decision = json.loads(pinned.stdout)["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "deny", pinned.stderr)
        broken = run([sys.executable, str(clone / "bin" / "dev"), "hook", "guard", "--agent", "claude"])
        self.assertNotIn("deny", broken.stdout)  # what the hooks ran before: the broken guard fails open

    def test_what_install_builds_from_the_pinned_copy_stays_writable(self):
        _, pin = self.pin_devkit()
        handoff = self.home / "Development" / ".handoff"
        handoff.mkdir(parents=True)
        git(handoff, "init", "-q", "-b", "main")
        market = self.home / ".local" / "share" / "devkit" / "codex-marketplace"
        with mock.patch.object(setup, "DEVKIT_HOME", pin), mock.patch.object(setup, "CODEX_MARKET", market), \
                mock.patch.object(setup, "HANDOFF", handoff), mock.patch.object(core, "handoff_repo", lambda: "ada/handoff"):
            setup.build_codex_plugin()
            setup.build_codex_plugin()  # the second build replaces the first: it must be able to delete it
            setup.setup_handoff()
        built = [market / "plugin"] + [Path(d) for d, _, _ in os.walk(market / "plugin")]
        self.assertTrue((market / "plugin" / "skills").is_dir())
        self.assertTrue(all(os.stat(d).st_mode & stat.S_IWUSR for d in built))
        copied = [f for f in handoff.iterdir() if f.is_file() and (pin / "templates" / "handoff" / f.name).exists()]
        self.assertTrue(copied)
        self.assertTrue(all(f.stat().st_mode & stat.S_IWUSR for f in copied), copied)  # the developer edits them


class OlderCopyTests(Sandbox):
    """An older devkit never overwrites a newer install. `self.old` is the pinned 26.11.3 that a session started
    before the update still runs; install.json pins `self.new`, 26.11.4."""

    def setUp(self):
        super().setUp()
        self.old = self.install()
        commit(self.src, "26.11.4")
        self.new = self.install(why="the next release")
        for p in (mock.patch.object(core, "DEVKIT_HOME", self.old), mock.patch.object(core, "VERSION", "26.11.3"),
                  mock.patch.object(hooks, "STATE", self.state)):
            p.start()
            self.addCleanup(p.stop)

    def skipped(self) -> list:
        return [r for r in self.audits() if r["action"] == "config.repair-skipped"]

    def test_an_older_pinned_copy_points_the_hooks_at_the_installed_release(self):
        self.assertEqual(core.runtime_home(), self.new)  # the status line and hook paths it would write
        setup.record_install(setup.select_pin("26.11.3"))  # rolled back: this copy is the installed one again
        self.assertEqual(core.runtime_home(), self.old)

    def test_background_commands_run_the_installed_release(self):
        (self.state / "logs").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(hooks.subprocess, "Popen") as popen:
            hooks._spawn(["config", "repair", "--quiet"])
        self.assertEqual(popen.call_args[0][0][3:], [str(self.new / "bin" / "dev"), "config", "repair", "--quiet"])

    def test_an_older_copy_repairs_nothing_and_records_both_versions(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{}\n")  # every managed key drifted
        self.assertEqual(policy.repair(quiet=True), [])
        self.assertEqual((self.home / ".claude" / "settings.json").read_text(), "{}\n")
        detail = self.skipped()[-1]["detail"]
        self.assertIn("26.11.3", detail)
        self.assertIn("26.11.4", detail)

    def test_an_older_copy_writes_no_settings_instructions_codex_config_or_git_ignore(self):
        git_calls = []
        FakeCodexServer.trusted, FakeCodexServer.hooks_file = {}, self.home / ".codex" / "hooks.json"
        with mock.patch.object(codexrpc, "CodexRPC", FakeCodexServer), \
                mock.patch.object(registry, "discover", lambda: ([], [], [])), \
                mock.patch.object(core, "gh_user", lambda: {"login": "ada", "name": "Ada", "id": 1}), \
                mock.patch.object(setup, "sh", lambda argv, **k: git_calls.append(argv) or (0, "", "")):
            self.assertFalse(setup.write_claude_settings())
            setup.write_agent_instructions(quiet=True)
            setup.write_codex_config(quiet=True)
            setup.setup_git(quiet=True)
        for f in (".claude/settings.json", ".claude/CLAUDE.md", ".codex/AGENTS.md", ".codex/hooks.json"):
            self.assertFalse((self.home / f).exists(), f)
        self.assertEqual(git_calls, [])
        self.assertEqual(len(self.skipped()), 4)

    def test_a_copy_equal_to_the_pin_writes_as_before(self):
        with mock.patch.object(core, "DEVKIT_HOME", self.new), mock.patch.object(core, "VERSION", "26.11.4"):
            self.assertTrue(setup.write_claude_settings())
        self.assertIn(str(self.new), self.settings()["statusLine"]["command"])
        self.assertEqual(self.skipped(), [])

    def test_an_unverified_pin_counts_by_its_release_numbers(self):
        self.assertEqual(core.version_key("26.12.0-dev"), (26, 12, 0))
        self.assertIsNone(core.version_key("main"))

    def test_install_json_that_names_a_copy_of_another_version_is_not_believed(self):
        rec = json.loads(core.install_record_file().read_text())
        core.install_record_file().write_text(json.dumps(dict(rec, version="99.1.0")))  # forged: a newer-looking pin
        with mock.patch.object(core, "DEVKIT_HOME", self.new), mock.patch.object(core, "VERSION", "26.11.4"):
            self.assertIsNone(core.older_copy())
        core.install_record_file().write_text(json.dumps(dict(rec, manifest_sha256="0" * 64)))  # another manifest
        self.assertIsNone(core.older_copy())
        self.assertEqual(core.release_home(), self.old)  # nothing believable is pinned: this copy runs its own


WHO_RUNS = "from pathlib import Path\n\n\ndef main():\n    print(Path(__file__).resolve().parents[2].name)\n    return 0\n"


def run_dev(copy: Path, home: Path, *args, older=False, tripwire=None):
    """`<copy>/bin/dev <args>` as a hook or a background job starts it, in the sandbox HOME -> CompletedProcess. The
    stand-ins come first on PATH (tests/_sandbox.py); an `older` release, whose core.py predates DEVKIT_TEST_PATH,
    starts through the net's launcher, which writes what it refused to `tripwire`."""
    env = _sandbox.env({"HOME": str(home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                        "LANG": "en_US.UTF-8", "DEVKIT_NO_NOTIFY": "1", **ISOLATED},
                       **({"DEVKIT_TEST_TRIPWIRE": tripwire} if tripwire else {}))
    dev = copy / "bin" / "dev"
    argv = _sandbox.boot(dev, *args) if older else [sys.executable, "-I", "-S", str(dev), *args]
    return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(home), timeout=300)


class RetireTests(Sandbox):
    """The other pinned copies once a newer release is installed. `self.old` (26.11.3) has devkit's real bin/dev and
    a cli that prints whose code runs; install.json pins `self.new` (26.11.4)."""

    def setUp(self):
        super().setUp()
        commit(self.src, "26.11.3", **{"bin/dev": (ROOT / "bin" / "dev").read_text(), "lib/devkit/cli.py": WHO_RUNS})
        self.old = self.install(why="the release before")
        commit(self.src, "26.11.4")
        self.new = self.install(why="the next release")

    def runs(self, copy: Path) -> str:
        p = run_dev(copy, self.home)
        return p.stdout.strip() or p.stderr

    def test_an_older_copy_runs_the_pinned_release_once_retired(self):
        self.assertEqual(self.runs(self.old), self.old.name)  # what a session that started before the install runs
        self.assertEqual(core.retire_other_pins(), [self.old.name])
        self.assertEqual(self.runs(self.old), self.new.name)
        self.assertEqual(self.runs(self.new), self.new.name)
        self.assertEqual(core.retire_other_pins(), [])  # once
        self.assertEqual(stat.S_IMODE((self.old / "bin").stat().st_mode), 0o555)  # read-only again
        self.assertEqual([r["target"] for r in self.audits() if r["action"] == "install.retire"], [self.old.name])

    def test_a_rollback_puts_the_copy_s_own_dev_back(self):
        core.retire_other_pins()
        self.assertIn("changed bin/dev", setup.verify_pin(self.old))
        self.assertTrue(core.restore_pin(self.old))
        self.assertEqual(setup.verify_pin(self.old), [])
        self.assertEqual(self.runs(self.old), self.old.name)
        self.assertFalse(core.restore_pin(self.old))  # nothing left to put back

    def test_dev_install_version_rolls_back_to_a_retired_copy(self):
        core.retire_other_pins()
        self.assertEqual(Path(setup.select_pin("26.11.3")["path"]), self.old)
        self.assertEqual(setup.verify_pin(self.old), [])
        self.assertEqual(self.runs(self.old), self.old.name)

    def test_nothing_is_retired_while_install_json_does_not_hold_up(self):
        rec = json.loads(core.install_record_file().read_text())
        core.install_record_file().write_text(json.dumps(dict(rec, manifest_sha256="0" * 64)))
        self.assertEqual(core.retire_other_pins(), [])
        self.assertFalse((self.old / "bin" / "dev").is_symlink())

    def test_the_pinned_release_s_repair_retires_the_others_and_an_older_copy_s_does_not(self):
        with mock.patch.object(policy, "diff", lambda **k: []):
            with mock.patch.object(core, "DEVKIT_HOME", self.old), mock.patch.object(core, "VERSION", "26.11.3"):
                policy.repair(quiet=True)
            self.assertFalse((self.old / "bin" / "dev").is_symlink())
            policy.repair(quiet=True)  # the checkout, which no hook runs
            self.assertFalse((self.old / "bin" / "dev").is_symlink())
            with mock.patch.object(core, "DEVKIT_HOME", self.new), mock.patch.object(core, "VERSION", "26.11.4"):
                policy.repair(quiet=True)
        self.assertEqual(core.retired_to(self.old), self.new)


def _has_release(tag: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "-q", f"{tag}^{{commit}}"],
                          capture_output=True, env=GIT_ENV).returncode == 0


@unittest.skipUnless(_has_release("v26.11.5"), "needs this checkout's v26.11.5 tag (git fetch --tags)")
class RealOlderReleaseTests(Sandbox):
    """The real devkit 26.11.5, which has no version check, pinned next to this working tree pinned as 26.11.6, and
    each run as an older session's hook runs it: `<copy>/bin/dev config repair --quiet`."""

    def pin_tree(self, name: str, version: str, files: bytes) -> Path:
        src = self.root / name
        src.mkdir()
        # the test extracts an archive it built itself, with the data filter when available
        with tarfile.open(fileobj=io.BytesIO(files)) as tf:  # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
            tf.extractall(str(src), **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        (src / "VERSION").write_text(version + "\n")
        git(src, "init", "-q", "-b", "main")
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", version)
        return self.install(src, why="a pinned-install test")

    def hook_copies(self) -> set:
        s = self.settings()
        cmds = [h["command"] for groups in s["hooks"].values() for g in groups for h in g["hooks"]]
        cmds.append(s["statusLine"]["command"])
        return {c.split("/releases/")[1].split("/")[0] for c in cmds if "/releases/" in c}

    def test_a_session_on_26_11_5_no_longer_reverts_the_26_11_6_install(self):
        parts = ["bin", "lib", "config", "agents", "VERSION"]
        old = self.pin_tree("v26.11.5", "26.11.5", subprocess.run(
            ["git", "-C", str(ROOT), "archive", "v26.11.5", *parts], capture_output=True, check=True).stdout)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for part in parts[:-1]:
                tf.add(str(ROOT / part), arcname=part, filter=lambda t: None if "__pycache__" in t.name else t)
        new = self.pin_tree("v26.11.6", "26.11.6", buf.getvalue())
        conf = self.home / ".config" / "devkit" / "config.json"
        conf.parent.mkdir(parents=True)
        conf.write_text(json.dumps({"agents": {"claude": True, "codex": False}}) + "\n")
        (self.home / ".claude").mkdir()
        claude_md = self.home / ".claude" / "CLAUDE.md"

        # what `dev install` of 26.11.6 writes; the old copy's bin/dev is still its own
        with mock.patch.object(core, "DEVKIT_HOME", new), mock.patch.object(core, "VERSION", "26.11.6"):
            setup.write_claude_settings()
            setup.write_agent_instructions(quiet=True)
        self.assertEqual(self.hook_copies(), {new.name})
        trips = self.root / "tripwire.jsonl"  # what the net refused the real 26.11.5: it must stay empty
        reverted = run_dev(old, self.home, "config", "repair", "--quiet", older=True, tripwire=trips)
        self.assertEqual(reverted.returncode, 0, reverted.stderr)
        self.assertIn(old.name, self.hook_copies())  # the finding: the real 26.11.5 writes its own hooks back
        self.assertIn("devkit 26.11.5 ", claude_md.read_text()[:200])

        repaired = run_dev(new, self.home, "config", "repair", "--quiet")  # the installed release (dev install's step)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(core.retired_to(old), new)
        self.assertEqual(self.hook_copies(), {new.name})

        for args in (("config", "repair", "--quiet"), ("config", "repair")):
            again = run_dev(old, self.home, *args, older=True, tripwire=trips)
            self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.hook_copies(), {new.name})
        self.assertEqual(_sandbox.trips(trips), [])
        self.assertIn("devkit 26.11.6 ", claude_md.read_text()[:200])


class SessionProcessTests(unittest.TestCase):
    """Claude Code gives a running conversation a new session id after a compaction, with no SessionStart: the first
    prompt registers it. Process 600 is the Claude Code process; its shells still say DEVKIT_SESSION=claude:old."""

    START = "Sat Nov  7 20:00:00 2026"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        table = {os.getpid(): (700, "python3 bin/dev hook prompt --agent claude"),
                 700: (600, "/bin/sh -c bin/dev hook prompt"), 600: (1, "claude --resume"), 1: (0, "/sbin/launchd")}
        for p in (mock.patch.object(hooks, "STATE", self.root), mock.patch.object(progress, "SESSIONS", self.sessions),
                  mock.patch.object(progress, "_ps", lambda: table),
                  mock.patch.object(approvals, "pid_start", lambda pid: self.START),
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:old"})):
            p.start()
            self.addCleanup(p.stop)
        self.adopted = []
        p = mock.patch.object(progress, "adopt_process_run", create=True,
                              side_effect=lambda *a: self.adopted.append(a))
        p.start()
        self.addCleanup(p.stop)
        self.record("claude", "old", pid=600, pid_start=self.START, started=time.time() - 3600)

    def record(self, agent, sid, **fields):
        (self.sessions / f"{agent}-{sid}.json").write_text(json.dumps(
            {"agent": agent, "session": sid, "cwd": str(self.root), "top": "", **fields}))

    def read(self, agent, sid) -> dict:
        return json.loads((self.sessions / f"{agent}-{sid}.json").read_text())

    def prompt(self, sid, agent="claude"):
        payload = {"session_id": sid, "cwd": str(self.root), "prompt": "continue", "hook_event_name": "UserPromptSubmit"}
        with mock.patch.object(hooks, "context_for", lambda *a, **k: ""), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.prompt(agent)

    def test_the_session_a_prompt_registers_holds_the_process_and_the_older_one_is_superseded(self):
        self.prompt("new")
        self.assertEqual((self.read("claude", "new")["pid"], self.read("claude", "new")["pid_start"]), (600, self.START))
        old = self.read("claude", "old")
        self.assertNotIn("pid", old)
        self.assertEqual((old["superseded_by"], old["pid_superseded"]), ("new", 600))
        self.assertEqual(self.adopted, [("claude", "new", 600)])  # the older session's run goes on in this one
        with mock.patch.object(progress, "_ancestor_pids", lambda: [4321, 700, 600, 1]):
            self.assertEqual(progress.resolve_session(), ("claude", "new", "ancestry"))
        self.prompt("new")
        self.assertEqual(len(self.adopted), 1)  # once

    def test_a_run_that_cannot_move_leaves_the_registration_in_place(self):
        with mock.patch.object(progress, "adopt_process_run", create=True, side_effect=OSError("busy")):
            self.prompt("new")
        self.assertEqual(self.read("claude", "new")["pid"], 600)
        self.assertEqual(self.read("claude", "old")["superseded_by"], "new")

    def test_a_record_an_earlier_prompt_made_without_its_process_gets_it(self):
        self.record("claude", "new", started=time.time())  # what the prompt hook wrote before this fix
        self.prompt("new")
        self.assertEqual(self.read("claude", "new")["pid"], 600)
        self.assertEqual(self.read("claude", "old")["superseded_by"], "new")

    def test_codex_threads_sharing_one_process_keep_their_pids(self):
        self.record("codex", "t1", pid=600, pid_start=self.START, started=time.time() - 60)
        hooks._register_session("codex", "t2", str(self.root))
        self.assertEqual((self.read("codex", "t1")["pid"], self.read("codex", "t2")["pid"]), (600, 600))
        self.assertEqual(self.read("claude", "old")["pid"], 600)  # another agent's record is not touched
        self.assertEqual(self.adopted, [])


class RestartListTests(unittest.TestCase):
    """dev install and dev update name the running Claude Code and Codex sessions that use devkit before they change
    anything: each keeps the hooks it started with until it is restarted."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sessions = Path(tmp.name) / "sessions"
        self.sessions.mkdir()
        self.record("claude", "aaaa1111", 101, project="acme/widget", branch="feat/report", devkit="26.11.4")
        self.record("codex", "bbbb2222", 102, project="acme/api", branch="main", devkit="26.11.4")
        self.record("claude", "cccc3333", 103, project="acme/old", branch="main")  # its process has ended
        self.alive = lambda pid, started=None: pid != 103

    def record(self, agent, sid, pid, **kw):
        rec = {"agent": agent, "session": sid, "pid": pid, "pid_start": "Sat Nov  7 09:00:00 2026", "started": 1, **kw}
        (self.sessions / f"{agent}-{sid}.json").write_text(json.dumps(rec))

    def confirm(self, **kw):
        lines = []
        go = hooks.confirm_restart(say=lines.append, sessions_dir=self.sessions, alive=self.alive, **kw)
        return go, "\n".join(lines)

    def test_with_yes_it_names_each_running_session_and_goes_on(self):
        go, text = self.confirm(assume_yes=True, ask=mock.Mock(side_effect=AssertionError("no question with --yes")))
        self.assertTrue(go)
        self.assertIn("Claude Code · acme/widget (feat/report) · devkit 26.11.4", text)
        self.assertIn("Codex · acme/api (main)", text)
        self.assertNotIn("acme/old", text)
        self.assertIn("restart", text)

    def test_in_a_terminal_it_asks_once_and_a_no_stops_before_any_change(self):
        ask = mock.Mock(return_value="n")
        self.assertEqual(self.confirm(interactive=True, ask=ask)[0], False)
        self.assertEqual(ask.call_count, 1)
        ask = mock.Mock(return_value="")
        self.assertEqual(self.confirm(interactive=True, ask=ask)[0], True)  # Enter goes on

    def test_without_running_sessions_it_says_and_asks_nothing(self):
        self.alive = lambda pid, started=None: False
        go, text = self.confirm(interactive=True, ask=mock.Mock(side_effect=AssertionError("nothing to ask")))
        self.assertEqual((go, text), (True, ""))


class InstallAsksFirstTests(Sandbox):
    """The answer to the restart list decides whether dev install and dev update change anything at all."""

    def test_install_stops_before_pinning_when_the_developer_says_no(self):
        answers = []

        def confirm(assume_yes=False):
            answers.append(assume_yes)
            return assume_yes  # the developer says no unless --yes
        with mock.patch.object(core, "source_home", lambda: self.src), mock.patch.object(hooks, "confirm_restart", confirm), \
                mock.patch("os.execv", side_effect=Execd) as execv, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.run_install(), 1)
            execv.assert_not_called()
            self.assertEqual((core.install_record(), core.pin_root().exists()), ({}, False))  # nothing pinned
            with self.assertRaises(Execd):
                cli.cmd_install(cli.build_parser().parse_args(["install", "--yes"]))
        self.assertEqual(answers, [False, True])
        self.assertTrue(core.install_record())

    def test_update_asks_before_it_moves_the_checkout(self):
        with mock.patch.object(hooks, "confirm_restart", lambda assume_yes=False: False), \
                mock.patch.object(cli, "update_checkout", side_effect=AssertionError("the checkout moved")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_update(cli.build_parser().parse_args(["update"]))
        self.assertEqual(rc, 1)
        self.assertIn("stopped before any change", out.getvalue())


class InstallRecordGuardTests(unittest.TestCase):
    """The version check trusts install.json, so no agent may write it or plant a pinned copy: the guard refuses."""

    def test_an_agent_cannot_write_install_json_or_plant_a_release(self):
        share = core.HOME / ".local" / "share" / "devkit"
        forged = json.dumps({"version": "99.1.0", "path": str(share / "releases" / "99.1.0-x")})
        cwd = str(core.DEV)
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                self.assertEqual(guard.check("Write", {"file_path": str(share / "install.json"), "content": forged},
                                             cwd, agent)[0], "deny")
                for cmd in (f"echo '{forged}' > ~/.local/share/devkit/install.json",
                            "mkdir -p ~/.local/share/devkit/releases/99.1.0-x",
                            "cp /tmp/x.json ~/.local/share/devkit/install.json",
                            "dev install --yes", "dev update --yes"):  # only dev install writes the record
                    self.assertEqual(guard.check("Bash", {"command": cmd}, cwd, agent)[0], "deny", cmd)


class InstallScriptTests(unittest.TestCase):
    """install.sh from a clone that is not a release says the pin waits for its tests, then leaves the last word to
    the wizard: its summary is the only one (INSTALL-AC-42). A pin `dev install` refuses shows there, as a step that
    failed (test_setup_flow.SummaryTests.test_a_refused_pin_is_not_ready_and_says_why)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_clone_says_the_pin_waits_for_its_tests_and_adds_nothing_after_the_wizard(self):
        clone = self.m.root / "work" / "devkit"
        subprocess.run(["git", "clone", "-q", str(self.m.remote), str(clone)], check=True, env=GIT_ENV,
                       capture_output=True)
        (clone / "install.sh").write_bytes((ROOT / "install.sh").read_bytes())
        (clone / "bin" / "dev").write_text(  # `dev setup` as far as it matters here: install.json and the copy
            "#!/usr/bin/env python3\nimport json, os\nhome = os.environ['HOME']\n"
            "pin = os.path.join(home, '.local/share/devkit/releases/26.12.0-dev-abc1234')\n"
            "os.makedirs(os.path.join(pin, 'bin'))\nopen(os.path.join(pin, 'bin', 'dev'), 'w').close()\n"
            "json.dump({'version': '26.12.0-dev', 'commit': 'abc1234def', 'path': pin},\n"
            "          open(os.path.join(home, '.local/share/devkit/install.json'), 'w'))\n")
        rc, out = self.m.run(["/bin/bash", str(clone / "install.sh"), "--skip-packages"])
        self.assertEqual(rc, 0, out)
        self.assertIn("not a release: dev install pins it for the hooks only after its tests pass here", out)
        after = out[out.index("5 of 5  Your setup"):]
        self.assertNotIn("Summary", after)
        self.assertNotIn("exit code", after)
        self.assertNotIn("read-only copy of devkit", after)


if __name__ == "__main__":
    unittest.main()
