# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A plan revision holds the plan's record lock until its commit, and the commit may wait up to 90 s for the handoff
repository's lock (work._repo_lock). A second revision of the same plan must wait as long, or it gives up while the
first is only waiting its turn. The handoff repository and devkit's state are temporary folders (no git: the commit
returns at once). Run: python3 -m unittest tests.test_work_plan_lock -v"""
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, plans, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
HELD_S = 90  # how long the other revision holds the plan's lock: its commit's full wait for the repository lock


class PlanRevisionWaitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        handoff = root / "handoff"
        for target in (mock.patch.object(core, "STATE", root / "state"),
                       mock.patch.object(core, "CONFIG_FILE", root / "config.json"),
                       mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(work, "HANDOFF", handoff),
                       mock.patch.object(plans, "HANDOFF", handoff), mock.patch.object(plans, "PLANS", handoff / "plans")):
            target.start()
            self.addCleanup(target.stop)
        self.plan = plans.new("acme/api", "Invoice export", a=A)

    def test_a_revision_waits_while_another_revision_of_the_plan_waits_for_the_repository(self):
        real, mine = work.record_lock, []

        @contextlib.contextmanager
        def held_by_another_revision(path, wait=work.RECORD_WAIT):
            if Path(path) == self.plan and not mine and wait <= HELD_S:  # reentrant once this revision holds it
                raise work.RecordBusyError(path, wait)
            with real(path, wait):
                mine.append(path)
                try:
                    yield
                finally:
                    mine.pop()

        with mock.patch.object(work, "record_lock", held_by_another_revision):
            try:
                meta = plans.revise(self.plan, "add the CSV columns", a=A)
            except work.RecordBusyError as e:
                self.fail(f"the revision gave up while the other one still waited its turn: {e}")
        self.assertEqual(meta["version"], 2)


if __name__ == "__main__":
    unittest.main()
