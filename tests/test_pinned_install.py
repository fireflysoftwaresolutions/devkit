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
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import audit, cli, codexrpc, core, gitx, policy, registry, setup  # noqa: E402
from test_install import Machine  # noqa: E402  (install.sh against stub brew, gh and a local repository)

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
            os.chmod(d, 0o755)
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
            self.assertEqual(execv.call_args[0][1][1:], [f"{pin}/bin/dev", "install", "--as-pinned"])
            commit(self.src, "26.11.4")  # untested: `--only` re-applies from the installed copy and pins nothing
            with self.assertRaises(Execd):
                setup.run_install(only={"claude"})
            self.assertEqual(execv.call_args[0][1][1:], [f"{pin}/bin/dev", "install", "--as-pinned", "--only", "claude"])
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
        os.chmod(pin, 0o755)
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
        s["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = f"'{core.DEVKIT_HOME}/bin/dev' hook guard --agent claude"
        (self.home / ".claude" / "settings.json").write_text(json.dumps(s))
        ok, detail, _ = self.checks()["hooks run the pinned copy"]
        self.assertFalse(ok)
        self.assertIn(f"Claude PreToolUse runs {core.tilde(core.DEVKIT_HOME)}", detail)


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
        env = {"HOME": str(self.home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "en_US.UTF-8",
               **ISOLATED}
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


class InstallScriptTests(unittest.TestCase):
    """install.sh ends by naming the pinned copy that the wizard's `dev install` made."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_summary_names_the_pinned_copy(self):
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
        self.assertIn("the hooks and the guard run a read-only copy of devkit 26.12.0-dev (abc1234) in "
                      "~/.local/share/devkit/releases/26.12.0-dev-abc1234", out)

    def test_the_summary_says_when_nothing_is_pinned(self):
        rc, out = self.m.pipe()
        self.assertEqual(rc, 0, out)
        self.assertIn("the hooks are not pinned yet: run dev install", out)


if __name__ == "__main__":
    unittest.main()
