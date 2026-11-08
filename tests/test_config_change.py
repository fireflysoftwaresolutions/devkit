# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The ConfigChange hook and DEVKIT_* variables (spec C11, K1): an agent that sets, changes or removes a DEVKIT_*
# variable in the env block of the user settings (through a shell, where the guard's file-tool rule does not see
# it) is refused with exit 2, the refusal is audited, and the variables go back to what devkit manages.
# Run: python3 -m unittest tests.test_config_change
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, hooks, policy  # noqa: E402


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
    # the user settings exactly as devkit manages them: no drift in any managed key
    flat, commands = policy.claude_desired()
    out = {}
    for path, v in flat.items():
        d = out
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = v
    out["hooks"] = {ev: [{"hooks": [{"type": "command", "command": c} for c in cs]}] for ev, cs in commands.items()}
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

    def test_the_hook_listens_to_project_and_local_settings_changes(self):
        import re
        root = Path(__file__).resolve().parents[1]
        entries = json.loads((root / "config" / "claude" / "settings.json").read_text())["hooks"]["ConfigChange"]
        for source in ("user_settings", "project_settings", "local_settings"):
            self.assertTrue(any(re.fullmatch(e.get("matcher") or ".*", source) for e in entries), source)


if __name__ == "__main__":
    unittest.main()
