# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The agent settings `dev install` writes: bypass mode, no AI attribution, and the defaults devkit recommends, for
Claude Code, Claude Desktop and Codex. Run: python3 -m unittest"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import desktop, setup  # noqa: E402


class ClaudeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.file = self.home / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, current: dict) -> dict:
        self.file.write_text(json.dumps(current))
        with mock.patch.object(setup, "CLAUDE_HOME", self.home), mock.patch.object(setup, "backup", lambda f: None), \
                mock.patch.dict(setup.CFG["claude"], {"default_mode": "bypassPermissions"}):
            setup.write_claude_settings(include_hooks=False)
        return json.loads(self.file.read_text())

    def test_every_attribution_is_off_and_the_deprecated_key_is_gone(self):
        s = self.install({"includeCoAuthoredBy": True, "theme": "dark",
                          "attribution": {"commit": "Co-Authored-By: Claude <noreply@anthropic.com>"},
                          "permissions": {"defaultMode": "default", "allow": ["Bash(ls:*)"]}})
        self.assertEqual(s["attribution"], {"commit": "", "pr": "", "sessionUrl": False})
        self.assertNotIn("includeCoAuthoredBy", s)
        self.assertEqual(s["permissions"], {"defaultMode": "bypassPermissions", "allow": ["Bash(ls:*)"]})
        self.assertIs(s["skipDangerousModePermissionPrompt"], True)
        self.assertEqual(s["theme"], "dark")  # the developer's own keys stay

    def test_long_builds_get_five_minutes_before_the_shell_times_out(self):
        s = self.install({"env": {"MY_VAR": "1"}})
        self.assertEqual(s["env"]["MY_VAR"], "1")
        self.assertEqual(s["env"]["BASH_DEFAULT_TIMEOUT_MS"], "300000")


class DesktopBypassTests(unittest.TestCase):
    """Claude Desktop keeps its own switch for bypass mode in the Code tab (Settings → Code), per account."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, account="acct-1", opt_in=None, gate=None):
        (self.base / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account,
                                                            "oauth:tokenCache": "never-read"}))
        prefs = {}
        if opt_in is not None:
            prefs["bypassPermissionsOptInByAccount"] = {"acct-1": opt_in, "acct-2": not opt_in}
        if gate is not None:
            prefs["bypassPermissionsGateByAccount"] = {"acct-1": gate}
        (self.base / "claude_desktop_config.json").write_text(json.dumps({"preferences": prefs}))
        return desktop.bypass_mode(self.base)

    def test_allowed_when_the_signed_in_account_opted_in(self):
        ok, detail = self.state(opt_in=True, gate=True)
        self.assertIs(ok, True)
        self.assertIn("allowed", detail)

    def test_off_names_the_switch_to_turn_on(self):
        ok, detail = self.state(opt_in=False, gate=True)
        self.assertIs(ok, False)
        self.assertIn("Settings → Code → Allow bypass permissions mode", detail)
        self.assertIs(self.state(gate=True)[0], False)  # never opted in

    def test_an_account_without_the_option_is_told_why(self):
        ok, detail = self.state(opt_in=True, gate=False)
        self.assertIs(ok, False)
        self.assertIn("not offered", detail)

    def test_unknown_without_a_signed_in_account(self):
        self.assertIsNone(self.state(account=None, opt_in=True)[0])
        self.assertIsNone(desktop.bypass_mode(self.base / "missing")[0])


class FakeCodex:
    """Codex's app-server, as far as the installer talks to it: it records the config edits."""
    edits = []
    hooks_file = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, edits, reload=True):
        FakeCodex.edits = list(edits)

    def trust_hooks(self, cwd, path, own):
        return 0

    def hooks(self, cwd):  # devkit's own hooks, trusted, as hooks/list reports them
        return [{"sourcePath": FakeCodex.hooks_file, "eventName": event, "handlerType": "command", "command": h["command"],
                 "matcher": g.get("matcher"), "timeoutSec": h.get("timeout"), "statusMessage": h.get("statusMessage"),
                 "trustStatus": "trusted"}
                for event, groups in setup.load_conf("codex/hooks.json")["hooks"].items() for g in groups
                for h in g["hooks"]]


class CodexSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_writes_full_bypass_and_turns_analytics_off(self):
        from devkit import codexrpc, registry
        FakeCodex.hooks_file = str(self.home / "hooks.json")
        with mock.patch.object(setup, "CODEX_HOME", self.home), mock.patch.object(setup, "backup", lambda f: None), \
                mock.patch.object(codexrpc, "CodexRPC", FakeCodex), \
                mock.patch.object(registry, "discover", lambda: ([], [], [])):
            setup.write_codex_config(quiet=True)
        edits = {k: (v, how) for k, v, how in FakeCodex.edits}
        self.assertEqual(edits["approval_policy"], ("never", "replace"))
        self.assertEqual(edits["sandbox_mode"], ("danger-full-access", "replace"))
        self.assertEqual(edits["analytics"], ({"enabled": False}, "upsert"))  # merged: the app's own keys stay

    def session(self, day: str, name: str, attribution) -> Path:
        d = self.home / "sessions" / "2026" / "11" / day
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"rollout-{name}.jsonl"
        meta = {"type": "turn_context", "payload": {"instructions": "x" * 150_000, "environments_instructions": False}}
        if attribution is not None:
            meta["payload"]["git_attribution"] = attribution
        f.write_text(json.dumps(meta) + "\n" + json.dumps({"type": "message", "text": "hello"}) + "\n")
        return f

    def test_codex_attribution_is_read_from_the_newest_session_that_records_it(self):
        sessions = self.home / "sessions"
        self.assertIsNone(setup.codex_git_attribution(sessions))  # no sessions yet: unknown
        old = self.session("04", "a", True)
        new = self.session("05", "b", False)
        import os
        os.utime(old, (1_000, 1_000))
        os.utime(new, (2_000, 2_000))
        self.assertIs(setup.codex_git_attribution(sessions), False)
        newest = self.session("06", "c", None)  # a session that does not record it is skipped
        os.utime(newest, (3_000, 3_000))
        self.assertIs(setup.codex_git_attribution(sessions), False)
        turned_on = self.session("06", "d", True)
        os.utime(turned_on, (4_000, 4_000))
        self.assertIs(setup.codex_git_attribution(sessions), True)


class IntelMacTests(unittest.TestCase):
    """Homebrew is in /usr/local on Intel Macs: devkit must not assume /opt/homebrew."""

    def test_the_prefix_follows_where_brew_is(self):
        from devkit import core
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bin").mkdir()
            (Path(d) / "bin" / "brew").write_text("")
            with mock.patch.dict("os.environ", {"HOMEBREW_PREFIX": d}):
                self.assertEqual(core.brew_prefix(), d)

    def test_docker_plugins_are_found_under_the_intel_prefix(self):
        from devkit import core
        with tempfile.TemporaryDirectory() as d, mock.patch.object(setup, "HOME", Path(d)), \
                mock.patch.object(core, "brew_prefix", lambda: "/usr/local"), \
                mock.patch.object(setup, "sh", lambda *a, **k: (1, "", "")), \
                mock.patch.object(setup, "backup", lambda f: None), mock.patch.object(setup, "_QUIET", [True]):
            try:
                setup.setup_docker()
            except Exception:
                pass  # the Colima part needs a real Mac; the plugin folder is written first
            cfg = json.loads((Path(d) / ".docker" / "config.json").read_text())
        self.assertIn("/usr/local/lib/docker/cli-plugins", cfg["cliPluginsExtraDirs"])


if __name__ == "__main__":
    unittest.main()
