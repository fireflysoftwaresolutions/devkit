# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What `dev config diff` and the ConfigChange repair call drift in the agents' settings (lib/devkit/policy.py).
Codex is a fake app-server that lists hooks as hooks/list does; nothing reads this Mac's ~/.codex.
Run: python3 -m unittest discover -s tests -t ."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import policy, setup  # noqa: E402

# A hook the developer (or an agent) added to ~/.codex/hooks.json next to devkit's own, on the same event.
FOREIGN = {"handlerType": "command", "command": "/usr/local/bin/my-audit", "matcher": None, "timeoutSec": 5,
           "async": False, "statusMessage": None, "additionalContextLimit": None}


class FakeRPC:
    """Codex's app-server for diff_codex: an empty config and the hooks it lists, in the order given."""

    def __init__(self, listed):
        self.listed = listed

    def read(self):
        return {"config": {}}

    def hooks(self, cwd):
        return self.listed


def devkit_hooks(hooks_file, status):
    """devkit's own hooks as hooks/list reports them, each with the trust status `status(event)` gives."""
    return [{"sourcePath": str(hooks_file), "eventName": event[0].lower() + event[1:], "handlerType": "command",
             "command": h["command"], "matcher": g.get("matcher"), "timeoutSec": h.get("timeout"), "async": False,
             "statusMessage": h.get("statusMessage"), "additionalContextLimit": h.get("additionalContextLimit"),
             "trustStatus": status(event)}
            for event, groups in setup.load_conf("codex/hooks.json")["hooks"].items() for g in groups
            for h in g["hooks"]]


class CodexHookTrustDriftTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.codex = Path(tmp.name) / ".codex"
        self.hooks_f = self.codex / "hooks.json"
        for p in (mock.patch.object(policy, "CODEX_HOME", self.codex),
                  mock.patch.object(policy, "codex_desired", lambda: {})):
            p.start()
            self.addCleanup(p.stop)

    def hook_drift(self, listed):
        return {d.key: (d.current, d.desired) for d in policy.diff_codex(FakeRPC(listed))}

    def test_an_untrusted_devkit_hook_is_drift_when_another_hook_on_its_event_is_trusted(self):
        listed = devkit_hooks(self.hooks_f, lambda ev: "untrusted" if ev == "PreToolUse" else "trusted")
        listed.append(dict(FOREIGN, sourcePath=str(self.hooks_f), eventName="preToolUse", trustStatus="trusted"))
        self.assertEqual(self.hook_drift(listed), {"hooks.PreToolUse": ("untrusted", "trusted")})

    def test_an_untrusted_foreign_hook_on_devkits_event_is_no_drift_of_devkits_hooks(self):
        listed = devkit_hooks(self.hooks_f, lambda ev: "trusted")
        listed.append(dict(FOREIGN, sourcePath=str(self.hooks_f), eventName="stop", trustStatus="untrusted"))
        self.assertEqual(self.hook_drift(listed), {})

    def test_a_devkit_hook_declared_another_way_is_missing(self):
        listed = devkit_hooks(self.hooks_f, lambda ev: "trusted")
        stop = next(h for h in listed if h["eventName"] == "stop")
        stop["timeoutSec"] = 999  # same command, changed declaration: Codex hashes it as another hook
        self.assertEqual(self.hook_drift(listed), {"hooks.Stop": (policy.MISSING, "trusted")})


if __name__ == "__main__":
    unittest.main()
