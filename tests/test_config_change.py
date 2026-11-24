# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The ConfigChange hook and DEVKIT_* variables (spec C11, K1): an agent that sets, changes or removes a DEVKIT_*
# variable in the env block of the user settings (through a shell, where no file tool's path shows
# it) is refused with exit 2, the refusal is audited, and the variables go back to what devkit manages.
# Run: python3 -m unittest tests.test_config_change
import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, hooks, policy, setup  # noqa: E402

REAL_SPAWN = hooks._spawn


def pin_release(home: Path, version: str, record_version: str = None) -> Path:
    # a pinned copy as dev install leaves it: a folder of the releases folder with its manifest, and install.json
    # naming it (record_version: what install.json claims, when it differs from the copy's own manifest)
    share = home / ".local" / "share" / "devkit"
    copy = share / "releases" / f"{version}-abc1234"
    copy.mkdir(parents=True)
    manifest = copy / core.PIN_MANIFEST
    manifest.write_text(json.dumps({"name": copy.name, "version": version, "commit": "abc1234", "files": {}}))
    rec = {"name": copy.name, "version": record_version or version, "commit": "abc1234", "path": str(copy),
           "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    (share / "install.json").write_text(json.dumps(rec))
    return copy


def managed_settings() -> dict:
    # the user settings exactly as devkit manages them: no drift in any managed key, its hooks as dev install writes
    # them (each with its matcher, type and timeout)
    flat, _ = policy.claude_desired()
    out = {}
    for path, v in flat.items():
        d = out
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = v
    out["hooks"] = copy.deepcopy(setup.load_conf("claude/settings.json")["hooks"])
    return out


class ConfigChangeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = root / ".claude" / "settings.json"
        self.settings.parent.mkdir()
        self.spawned = []
        self.root = root
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"), mock.patch.object(policy, "STATE", root / "state"),
                  mock.patch.object(hooks, "STATE", root / "state"), mock.patch.object(core, "HOME", root),
                  mock.patch.object(hooks, "_spawn", lambda args, *a, **k: self.spawned.append(args))):
            p.start()
            self.addCleanup(p.stop)

    def change(self, settings):
        self.settings.write_text(json.dumps(settings, indent=2))
        payload = {"session_id": "s1", "hook_event_name": "ConfigChange", "source": "user_settings",
                   "file_path": str(self.settings)}
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stderr(err):
            try:
                hooks.config_change("claude")
                code = 0
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_a_devkit_variable_added_to_the_user_settings_is_refused_and_removed(self):
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        code, err = self.change(s)
        self.assertEqual(code, 2)
        self.assertIn("DEVKIT_GUARD", err)
        env = json.loads(self.settings.read_text()).get("env") or {}
        self.assertNotIn("DEVKIT_GUARD", env)
        self.assertEqual(env, managed_settings().get("env") or {})  # the other variables are left as they were
        self.assertIn("DEVKIT_GUARD", audit.records(1, action="config.blocked")[-1]["detail"])

    def test_user_settings_without_devkit_variables_pass(self):
        self.assertEqual(self.change(managed_settings()), (0, ""))

    def test_removed_user_settings_are_refused_like_an_empty_file_and_repaired(self):
        self.settings.write_text("{}")
        empty = self.change_written()
        self.settings.unlink()
        code, err = self.change_written()
        self.assertEqual((code, err), empty)
        self.assertEqual(code, 2)
        self.assertIn("devkit manages these settings", err)
        self.assertEqual(self.spawned, [["config", "repair", "--quiet"]] * 2)

    def test_user_settings_that_do_not_open_are_refused_and_left_alone(self):
        self.settings.mkdir()  # there, but no file devkit can open: Claude Code's own read is unknown
        code, err = self.change_written()
        self.assertEqual(code, 2)
        self.assertIn("(it does not open: IsADirectoryError)", err)
        self.assertEqual(audit.records(1, action="config.blocked")[-1]["detail"], "unreadable")
        self.assertEqual(self.spawned, [])

    def test_unreadable_user_settings_are_refused_when_devkit_configuration_fails_too(self):
        self.settings.write_bytes(b'{"env": {"DEVKIT_GUARD": "off"}, "note": "caf\xe9"}')
        with mock.patch.object(policy, "claude_desired", side_effect=KeyError("hooks")):
            code, err = self.change_written()
        self.assertEqual(code, 2)
        self.assertIn("(it holds a byte that is not UTF-8)", err)
        self.assertIn("devkit's own configuration also failed to load (KeyError)", err)
        self.assertEqual(audit.records(1, action="config.blocked")[-1]["detail"], "unreadable")
        self.assertEqual(self.spawned, [])  # a file devkit cannot read it never rewrites

    def test_readable_user_settings_are_refused_when_devkit_configuration_fails(self):
        # a hook that crashed would exit without blocking, so the change would pass unchecked
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        self.settings.write_text(json.dumps(s))
        before = self.settings.read_bytes()
        with mock.patch.object(policy, "claude_desired", side_effect=KeyError("hooks")):
            code, err = self.change_written()
        self.assertEqual(code, 2)
        self.assertIn("devkit's own configuration failed to load (KeyError)", err)
        self.assertIn("Run dev doctor.", err)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(audit.records(1, action="config.blocked")[-1]["detail"], "unchecked: devkit's configuration")
        self.assertEqual(self.spawned, [])

    def test_readable_user_settings_are_refused_when_the_managed_keys_check_fails(self):
        before = json.dumps(managed_settings())
        self.settings.write_text(before)
        with mock.patch.object(policy, "diff_claude", side_effect=TypeError("unhashable")):
            code, err = self.change_written()
        self.assertEqual(code, 2)
        self.assertIn("devkit could not check the settings it manages", err)
        self.assertIn("(TypeError)", err)
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(audit.records(1, action="config.blocked")[-1]["detail"], "unchecked")
        self.assertEqual(self.spawned, [])

    def change_written(self):
        """The ConfigChange hook on the user settings as they are on disk -> (exit code, stderr)."""
        payload = {"session_id": "s1", "hook_event_name": "ConfigChange", "source": "user_settings",
                   "file_path": str(self.settings)}
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stderr(err):
            try:
                hooks.config_change("claude")
                code = 0
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def project_change(self, name, source, settings):
        f = self.settings.parent.parent / "repo" / ".claude" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(settings, indent=2))
        payload = {"session_id": "s1", "hook_event_name": "ConfigChange", "source": source, "file_path": str(f)}
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stderr(err):
            try:
                hooks.config_change("claude")
                code = 0
            except SystemExit as e:
                code = e.code
        return code, err.getvalue(), json.loads(f.read_text())

    def test_a_devkit_variable_in_the_project_or_local_settings_is_refused_and_removed(self):
        # spec C11: a DEVKIT_* key in the env block of any settings file, written through a shell
        for name, source in (("settings.json", "project_settings"), ("settings.local.json", "local_settings")):
            with self.subTest(source):
                code, err, after = self.project_change(name, source, {
                    "permissions": {"allow": ["Bash(make test)"]},
                    "env": {"DEVKIT_FROM_AGENT": "", "NODE_ENV": "test"}})
                self.assertEqual(code, 2)
                self.assertIn("DEVKIT_FROM_AGENT", err)
                self.assertEqual(after, {"permissions": {"allow": ["Bash(make test)"]}, "env": {"NODE_ENV": "test"}})
                self.assertIn("DEVKIT_FROM_AGENT", audit.records(1, action="config.blocked")[-1]["detail"])

    def test_project_settings_without_devkit_variables_pass_untouched(self):
        # the project's own settings hold none of devkit's managed keys: only the DEVKIT_* rule applies to them
        settings = {"permissions": {"allow": ["Bash(make test)"]}, "env": {"NODE_ENV": "test"}}
        self.assertEqual(self.project_change("settings.json", "project_settings", settings), (0, "", settings))
        self.assertEqual(self.spawned, [])

    # An older devkit never overwrites a newer install: a session started before `dev install` pinned a newer release
    # still runs the older copy's hooks. Its ConfigChange hook sees the newer settings as drift.
    def older_session(self, running="26.11.4", pinned="26.11.5"):
        pin_release(self.root, pinned)
        p = mock.patch.object(core, "VERSION", running)
        p.start()
        self.addCleanup(p.stop)

    def newer_install_settings(self) -> dict:
        # what the newer install wrote: other hook commands, and a moment without devkit's plugin
        s = managed_settings()
        s["hooks"] = {ev: [{"hooks": [{"type": "command", "command": "'/new/bin/dev' hook x"}]}] for ev in s["hooks"]}
        s.get("enabledPlugins", {}).pop("devkit@firefly-devkit", None)
        return s

    def test_an_older_copy_rewrites_nothing_and_hands_the_repair_to_the_pinned_release(self):
        self.older_session()
        s = self.newer_install_settings()
        with mock.patch.object(policy, "lock_status", lambda: "off"), contextlib.redirect_stdout(io.StringIO()):
            code, err = self.change(s)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.settings.read_text()), s)  # the newer settings stay as written
        self.assertEqual(self.spawned, [["config", "repair", "--quiet"]])  # run by the pinned release (_spawn)
        self.assertEqual(audit.records(1, action="config.blocked"), [])
        rec = audit.records(1, action="config.repair-skipped")[-1]
        self.assertIn("26.11.4", rec["detail"])
        self.assertIn("26.11.5", rec["detail"])

    def test_an_older_copy_tells_the_session_once_to_restart(self):
        self.older_session()
        out = io.StringIO()
        with mock.patch.object(policy, "lock_status", lambda: "off"), contextlib.redirect_stdout(out):
            self.change(self.newer_install_settings())
            first = out.getvalue()
            self.change(self.newer_install_settings())
        msg = json.loads(first)["systemMessage"]
        self.assertIn("devkit 26.11.4", msg)
        self.assertIn("26.11.5", msg)
        self.assertIn("Restart", msg)
        self.assertEqual(out.getvalue(), first)  # the second change says nothing more
        self.assertEqual(len(audit.records(1, action="config.repair-skipped")), 2)  # each skip is recorded

    def test_an_older_copy_still_refuses_a_devkit_variable_without_writing_the_file(self):
        # an agent in an older session cannot use the version gap to switch a safeguard off: the change is refused
        # (exit 2) and the pinned release's repair puts devkit's values back
        self.older_session()
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        with contextlib.redirect_stdout(io.StringIO()):
            code, err = self.change(s)
        self.assertEqual(code, 2)
        self.assertIn("DEVKIT_GUARD", err)
        self.assertEqual(json.loads(self.settings.read_text())["env"]["DEVKIT_GUARD"], "off")  # older code wrote nothing
        self.assertEqual(self.spawned, [["config", "repair", "--quiet"]])

    def test_a_copy_equal_to_the_pin_repairs_as_before(self):
        self.older_session(running="26.11.5", pinned="26.11.5")
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        code, _ = self.change(s)
        self.assertEqual(code, 2)
        self.assertNotIn("DEVKIT_GUARD", json.loads(self.settings.read_text()).get("env") or {})
        self.assertEqual(audit.records(1, action="config.repair-skipped"), [])

    def test_a_forged_newer_version_in_install_json_does_not_stop_the_repair(self):
        # install.json claims 99.1.0, but the copy it names says 26.11.5 in its own manifest: the record is not
        # believed, so this copy repairs as before
        pin_release(self.root, "26.11.5", record_version="99.1.0")
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        with mock.patch.object(core, "VERSION", "26.11.5"):
            code, _ = self.change(s)
        self.assertEqual(code, 2)
        self.assertNotIn("DEVKIT_GUARD", json.loads(self.settings.read_text()).get("env") or {})
        self.assertEqual(audit.records(1, action="config.repair-skipped"), [])

    def test_the_session_start_of_an_older_copy_records_the_skip_and_tells_the_developer(self):
        self.older_session()
        payload = {"session_id": "s2", "hook_event_name": "SessionStart", "cwd": str(self.root)}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch.object(hooks, "context_for", lambda cwd, agent: "context"), \
                mock.patch.object(hooks, "_register_session", lambda *a, **k: self.root / "state" / "s2.json"), \
                mock.patch.object(hooks, "_continuity", lambda *a, **k: ""), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None):
            hooks.session_start("claude")
        self.assertIn("Restart", json.loads(out.getvalue())["systemMessage"])
        self.assertIn("26.11.5", audit.records(1, action="config.repair-skipped")[-1]["detail"])

    # The refusal is the control: whatever devkit's logging, its background repair or its own code does around it, a
    # change it refuses exits 2. hooks.run turns any other exception into exit 0, which lets the change pass.
    def unlocked(self):
        p = mock.patch.object(policy, "lock_status", lambda: "off")
        p.start()
        self.addCleanup(p.stop)

    def drifted(self) -> dict:
        s = managed_settings()
        s.pop("permissions")
        return s

    def test_a_refusal_holds_when_its_logs_or_its_repair_do_not_open(self):
        logs = self.root / "state" / "logs"
        started = []
        cases = {"config.log is a folder": lambda: (logs / "config.log").mkdir(parents=True),
                 "sync.log is a folder": lambda: (logs / "sync.log").mkdir(parents=True),
                 "logs is a file": lambda: (logs.parent.mkdir(parents=True, exist_ok=True), logs.write_text("x"))}
        for name, break_logs in cases.items():
            with self.subTest(name), mock.patch.object(hooks, "_spawn", REAL_SPAWN), \
                    mock.patch.object(hooks.subprocess, "Popen", lambda *a, **k: started.append(a)):
                break_logs()
                code, err = self.change(self.drifted())
                self.assertEqual(code, 2, err)
                self.assertIn("devkit manages these settings: permissions.", err)
                self.assertEqual(audit.records(1, action="config.blocked")[-1]["detail"][:12], "permissions.")
                shutil.rmtree(logs) if logs.is_dir() else logs.unlink()
        self.assertEqual(len(started), 1)  # the repair starts when its log opens: with config.log a folder

    def test_an_older_copy_still_refuses_a_devkit_variable_when_its_repair_does_not_open(self):
        self.older_session()
        (self.root / "state" / "logs" / "sync.log").mkdir(parents=True)
        s = managed_settings()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        with mock.patch.object(hooks, "_spawn", REAL_SPAWN), contextlib.redirect_stdout(io.StringIO()):
            code, err = self.change(s)
        self.assertEqual(code, 2, err)
        self.assertIn("refused env.DEVKIT_GUARD", err)

    def test_a_check_that_fails_unexpectedly_refuses_the_change(self):
        with mock.patch.object(core, "older_copy", side_effect=RuntimeError("broken")):
            code, err = self.change(self.drifted())
        self.assertEqual(code, 2)
        self.assertIn("(RuntimeError)", err)
        self.assertIn("refused the change", err)

    # devkit's hooks count only as dev install writes them: under a matcher that never matches, as another type, with
    # another timeout or run in the background, they never stop anything, and disableAllHooks turns them all off.
    def test_devkit_hooks_that_differ_from_what_devkit_writes_are_refused(self):
        self.unlocked()

        def under(change):
            s = managed_settings()
            for group in s["hooks"]["PreToolUse"]:
                change(group, group["hooks"][0])
            return s

        cases = {"a matcher that never matches": lambda g, h: g.update(matcher="NoSuchTool"),
                 "no matcher where devkit has one": lambda g, h: g.pop("matcher"),
                 "a type that is not command": lambda g, h: h.update(type="prompt"),
                 "no type": lambda g, h: h.pop("type"),
                 "another timeout": lambda g, h: h.update(timeout=0.001),
                 "run in the background": lambda g, h: h.update({"async": True})}
        for name, change in cases.items():
            with self.subTest(name):
                code, err = self.change(under(change))
                self.assertEqual(code, 2, name)
                self.assertIn("devkit manages these settings: hooks.PreToolUse.", err)
        with self.subTest("a matcher where devkit has none, matching everything"):
            s = managed_settings()
            s["hooks"]["Stop"][0]["matcher"] = "*"
            self.assertEqual(self.change(s), (0, ""))

    def test_disable_all_hooks_is_refused_with_the_lock_on_or_off(self):
        for lock in ("off", "on"):
            for value in (True, "true", 1):
                with self.subTest(lock=lock, value=value), mock.patch.object(policy, "lock_status", lambda: lock):
                    s = managed_settings()
                    s["disableAllHooks"] = value
                    code, err = self.change(s)
                    self.assertEqual(code, 2)
                    self.assertIn("devkit manages these settings: disableAllHooks.", err)
        self.unlocked()
        s = managed_settings()
        s["disableAllHooks"] = False
        self.assertEqual(self.change(s), (0, ""))

    def test_repair_drops_disable_all_hooks_and_puts_devkit_hooks_back_as_it_writes_them(self):
        s = managed_settings()
        s["disableAllHooks"] = True
        s["hooks"]["PreToolUse"][0]["matcher"] = "NoSuchTool"
        s["hooks"]["Stop"][0]["hooks"][0]["type"] = "prompt"
        self.settings.write_text(json.dumps(s))
        self.assertEqual(sorted(d.key for d in policy.diff_claude(s) if d.key[:5] in ("hooks", "disab")),
                         ["disableAllHooks", "hooks.PreToolUse", "hooks.Stop"])
        with mock.patch.object(setup, "CLAUDE_HOME", self.settings.parent), \
                mock.patch.object(setup, "STATE", self.root / "state"):
            setup.write_claude_settings()
            after = json.loads(self.settings.read_text())
            self.assertNotIn("disableAllHooks", after)
            self.assertEqual([d.key for d in policy.diff_claude(after) if d.key[:5] in ("hooks", "disab")], [])
            after["disableAllHooks"] = False  # false turns nothing off: it stays as the developer wrote it
            self.settings.write_text(json.dumps(after))
            setup.write_claude_settings()
            self.assertIs(json.loads(self.settings.read_text())["disableAllHooks"], False)

    # Claude Code reads its settings with Node's JSON.parse, which refuses NaN and Infinity; Python's json reads them.
    def test_the_hook_listens_to_project_and_local_settings_changes(self):
        import re
        root = Path(__file__).resolve().parents[1]
        entries = json.loads((root / "config" / "claude" / "settings.json").read_text())["hooks"]["ConfigChange"]
        for source in ("user_settings", "project_settings", "local_settings"):
            self.assertTrue(any(re.fullmatch(e.get("matcher") or ".*", source) for e in entries), source)


if __name__ == "__main__":
    unittest.main()
