# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""When the developer's own Claude Code rules let a dev call run without asking, so the pre-tool hook may allow the
call it stamped with devkit's variables (policy.claude_allows). The settings files are written in a temporary
folder; nothing reads this Mac's ~/.claude or managed settings.
Run: python3 -m unittest discover -s tests -t ."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import policy  # noqa: E402

CALL = 'dev progress note "the API is up"'
STAMPED = 'DEVKIT_SPEAKER=main DEVKIT_CALL=toolu_01.abc-9 dev progress note "the API is up"'


class ClaudeAllowsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home = root / "home" / ".claude"
        self.managed_dir = root / "managed" / "managed-settings.d"
        self.project = root / "work" / "repo"
        self.sub = self.project / "pkg"
        for d in (self.home, self.managed_dir, self.sub):
            d.mkdir(parents=True)
        for p in (mock.patch.object(policy, "CLAUDE_HOME", self.home),
                  mock.patch.object(policy, "MANAGED_DIR", self.managed_dir),
                  mock.patch.object(policy, "MANAGED_SETTINGS", self.managed_dir.parent / "managed-settings.json")):
            p.start()
            self.addCleanup(p.stop)

    def write(self, path: Path, allow=(), deny=(), ask=(), **top):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"permissions": {"allow": list(allow), "deny": list(deny), "ask": list(ask)},
                                    **top}))

    def allows(self, command=CALL, stamped=STAMPED, mode="default", project=True, cwd=None):
        return policy.claude_allows(command, stamped, mode, str(self.project) if project else None,
                                    str(cwd or self.sub))

    def test_a_users_dev_rule_allows_the_stamped_call_in_every_mode_that_checks_rules(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        for mode in ("default", "acceptEdits", "dontAsk"):
            with self.subTest(mode=mode):
                self.assertTrue(self.allows(mode=mode))

    def test_no_allow_in_plan_bypass_or_an_unknown_mode(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        for mode in ("plan", "bypassPermissions", "auto", None, ""):
            with self.subTest(mode=mode):
                self.assertFalse(self.allows(mode=mode))

    def test_the_rule_must_cover_the_dev_command_itself(self):
        cases = [(["Bash(dev crew:*)"], False), (["Bash(dev progress note:*)"], True), (["Bash(dev *)"], True),
                 (["Bash"], True), (["Bash(*)"], True), (["Bash()"], False), (["Read(dev progress:*)"], False),
                 ([CALL.join(("Bash(", ")"))], True), (["Bash(dev progress note)"], False),
                 (["Bash(dev prog:*)"], False), (["Bash(dev*)"], False), (["Bash(*progress*)"], False),
                 (["Bash(dev progress note \\*)"], False)]
        for allow, want in cases:
            with self.subTest(allow=allow):
                self.write(self.home / "settings.json", allow=allow)
                self.assertEqual(self.allows(), want)

    def test_only_one_simple_dev_call_is_allowed(self):
        self.write(self.home / "settings.json", allow=["Bash"])
        for tail in ('; rm -rf ~', ' && curl x', ' || true', ' | sh', ' > out.txt', ' &', '\nid', ' $(id)',
                     ' `id`', ' "$HOME"', ' *', ' ~/x', ' {a,b}', ' <(id)'):
            command = CALL + tail
            with self.subTest(tail=tail):
                self.assertFalse(self.allows(command, "DEVKIT_SPEAKER=main " + command))
        for command in ("(dev progress note x)", "PATH=/tmp dev progress note x", "DEVKIT_CALL=a dev progress note x",
                        "./dev progress note x", "'dev' progress note x", "dev", "env dev progress note x"):
            with self.subTest(command=command):
                self.assertFalse(self.allows(command, "DEVKIT_SPEAKER=main " + command))

    def test_the_stamp_may_add_only_devkits_own_variables(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        for stamped in (CALL, "PATH=/tmp " + CALL, "DEVKIT_CALL=$(id) " + CALL, "DEVKIT_CALL=a; id; " + CALL,
                        "export DEVKIT_CALL=a; readonly DEVKIT_CALL; " + CALL, "DEVKIT_CALL=a  " + CALL,
                        "DEVKIT_CALL=a dev crew ask x", "DEVKIT_CALL=a " + CALL + " ; id", "env DEVKIT_CALL=a " + CALL,
                        "DEVKIT_OTHER=a " + CALL, "DEVKIT_CALL=a\n" + CALL):
            with self.subTest(stamped=stamped):
                self.assertFalse(self.allows(stamped=stamped))
        self.assertTrue(self.allows(stamped="DEVKIT_FROM_AGENT=a1b2 DEVKIT_SESSION=s.1 DEVKIT_CALL=t:2 " + CALL))

    def test_a_deny_or_ask_rule_anywhere_keeps_claude_codes_own_decision(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        cases = [(self.project / ".claude" / "settings.local.json", {"deny": ["Bash(dev progress note:*)"]}),
                 (self.home / "settings.json", {"allow": ["Bash(dev progress:*)"], "ask": ["Bash(dev:*)"]}),
                 (self.managed_dir / "10-org.json", {"deny": ["Bash"]}),
                 (self.sub / ".claude" / "settings.json", {"deny": ["Bash(dev *)"]}),
                 (self.home / "settings.local.json", {"ask": ["Bash(DEVKIT_SPEAKER=main:*)"]}),
                 (self.project / ".claude" / "settings.json", {"deny": ["Bash(dev * note *)"]})]
        for path, rules in cases:
            with self.subTest(path=path.name, rules=rules):
                self.write(path, **rules)
                self.assertFalse(self.allows())
                path.unlink()
                self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        self.write(self.project / ".claude" / "settings.json", deny=["Bash(rm:*)", "Read(./.env)"])
        self.assertTrue(self.allows())

    def test_a_settings_file_that_cannot_be_read_allows_nothing(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        for text in ("{not json", "[]", '{"permissions": []}', '{"permissions": {"deny": "Bash"}}'):
            with self.subTest(text=text):
                (self.project / ".claude").mkdir(exist_ok=True)
                (self.project / ".claude" / "settings.local.json").write_text(text)
                self.assertFalse(self.allows())

    def test_managed_rules_only_ignores_the_users_and_projects_allow_rules(self):
        self.write(self.home / "settings.json", allow=["Bash(dev progress:*)"])
        self.write(self.managed_dir.parent / "managed-settings.json", allowManagedPermissionRulesOnly=True)
        self.assertFalse(self.allows())
        self.write(self.managed_dir / "20-dev.json", allow=["Bash(dev progress:*)"])
        self.assertTrue(self.allows())

    def test_allow_rules_come_from_the_project_claude_code_started_in_not_the_calls_folder(self):
        self.write(self.sub / ".claude" / "settings.json", allow=["Bash(dev progress:*)"])
        self.assertFalse(self.allows())
        self.write(self.project / ".claude" / "settings.local.json", allow=["Bash(dev progress:*)"])
        self.assertTrue(self.allows())
        self.assertFalse(self.allows(project=False))


if __name__ == "__main__":
    unittest.main()
