# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The ConfigChange hook and DEVKIT_* variables (spec C11, K1): an agent that sets, changes or removes a DEVKIT_*
# variable in the env block of the user settings (through a shell, where the guard's file-tool rule does not see
# it) is refused with exit 2, the refusal is audited, and the variables go back to what devkit manages.
# Run: python3 -m unittest tests.test_config_change
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, hooks, policy  # noqa: E402


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
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"), mock.patch.object(policy, "STATE", root / "state"),
                  mock.patch.object(hooks, "STATE", root / "state"),
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


if __name__ == "__main__":
    unittest.main()
