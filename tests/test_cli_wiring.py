# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The command line reaches the domain code: `dev config set <key> <value>` writes one key through core.config_set
# (crew slots R1), and `dev progress step --approves "<steps>"` records the step's verdict through approvals.record
# (spec F6). Each test drives cli.main as a terminal would, in a temporary state; nothing below the CLI is stubbed
# except whether the caller is the developer's terminal.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_wiring
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import audit, cli, core, progress as pg  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

REPORT = ("**[Firefly> DevKit · Release engineer (Link)]** api · `main` · tag\n- **Done:** tagged v1\n"
          "- **Check:** ✓ the tag points at the verified commit\n- **Next:** Morpheus")


def dev(*argv):
    # cli.main as the main session's terminal runs it: no subagent lane, its output captured
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        os.environ.pop("DEVKIT_FROM_AGENT", None)
        rc = cli.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class ConfigSetTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.file = Path(self.state.tmp.name) / "config.json"
        p = mock.patch.object(core, "CONFIG_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)

    def as_developer(self, yes):
        return mock.patch.object(core, "set_by_developer", lambda: yes)

    def test_the_developer_sets_a_key_and_the_change_is_written_and_audited(self):
        with self.as_developer(True):
            rc, out = dev("config", "set", "progress.max_parallel", "6")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.file.read_text()), {"progress": {"max_parallel": 6}})
        self.assertIn("progress.max_parallel: 4 → 6", out)
        self.assertEqual([r["target"] for r in audit.records(1, action="dev.config.set")], ["progress.max_parallel"])

    def test_an_agents_set_is_refused_and_writes_nothing(self):
        with self.as_developer(False):
            rc, out = dev("config", "set", "progress.max_parallel", "6")
        self.assertEqual(rc, 2)
        self.assertIn("only the developer", out)
        self.assertFalse(self.file.exists())

    def test_set_without_a_value_shows_the_usage_and_writes_nothing(self):
        with self.as_developer(True):
            rc, out = dev("config", "set", "progress.max_parallel")
        self.assertEqual(rc, 2)
        self.assertIn("dev config set <key> <value>", out)
        self.assertFalse(self.file.exists())


class StepApprovesTests(HookCase):
    def setUp(self):
        super().setUp()
        pg.start("Release", plan="tag=10m; verify=10m after tag")
        pg.assign("tag", "Link", activity="release")
        launch("a_link", "devkit:link", "Link: tag")
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_link", "agent_type": "devkit:link",
                                  "last_assistant_message": REPORT}, False, now=time.time() + 1)

    def test_the_accountable_approves_a_finished_step_with_the_check_of_their_own_step(self):
        rc, out = dev("progress", "step", "verified the release", "--step", "verify",
                      "--check", "✓ downloaded the release and ran it", "--approves", "tag")
        self.assertEqual(rc, 0, out)
        run = pg.load_run("claude", "s1")
        self.assertEqual([(a["by"], a["steps"], a["verdict"]) for a in run.get("approvals") or []],
                         [("morpheus", ["tag"], "approved")])

    def test_morpheus_approving_his_own_step_is_refused_and_records_no_approval(self):
        rc, out = dev("progress", "step", "verified the release", "--step", "verify",
                      "--check", "✓ downloaded the release", "--approves", "verify")
        self.assertEqual(rc, 2, out)
        self.assertIn("verify", out)
        self.assertEqual(pg.load_run("claude", "s1").get("approvals") or [], [])


if __name__ == "__main__":
    unittest.main()
