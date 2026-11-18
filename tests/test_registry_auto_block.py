# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's note keeps its auto block steady: each Mac with a clone writes only its own part of it, so two Macs
never trade the block's lines at every sync, and a session starting or ending in a checkout changes nothing in it
(the handoff repository took a commit for each). The handoff repository, devkit's state and work items are
temporary folders; the Mac's identity is a stand-in. Run: python3 -m unittest tests.test_registry_auto_block -v"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, identity, registry, work  # noqa: E402

PID = "acme/weave"
DAY = 86400


class AutoBlockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        state, handoff = self.root / "state", self.root / "handoff"
        for target in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state),
                       mock.patch.object(core, "CONFIG_FILE", self.root / "config.json"),
                       mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(registry, "HANDOFF", handoff),
                       mock.patch.object(core, "NOTES", handoff / "projects"),
                       mock.patch.object(registry, "NOTES", handoff / "projects"),
                       mock.patch.object(work, "HANDOFF", handoff), mock.patch.object(work, "ITEMS", handoff / "items"),
                       mock.patch.object(work, "SESSIONS", handoff / "sessions"),
                       mock.patch.object(work, "request_sync", lambda *a, **k: False),
                       mock.patch.object(audit, "AUDIT", state / "audit")):
            target.start()
            self.addCleanup(target.stop)
        self.clone = self.root / "Development" / "acme" / "weave"
        self.clone.mkdir(parents=True)
        self.note = handoff / "projects" / "acme" / "weave.md"

    @staticmethod
    def on(machine_id: str, label: str):
        return mock.patch.object(identity, "machine", lambda now=None: identity.Machine(machine_id, label, {}))

    def project(self, stack: str, main=None, worktrees=(), branches=(), local=True) -> dict:
        return {"id": PID, "path": str(self.clone) if local else None, "local": local, "canonical_ok": True,
                "stack": [stack], "commands": ["make test"], "main": main, "worktrees": list(worktrees),
                "branches": list(branches)}

    def block(self) -> str:
        text = self.note.read_text()
        return text[text.index(registry.AUTO_START):text.index(registry.AUTO_END)]

    def test_two_macs_with_a_clone_each_keep_their_own_part_instead_of_trading_the_block(self):
        with self.on("m-aaaaaaaa", "mbp-ana"):
            self.assertTrue(registry.update_note(self.project("python")))
        with self.on("m-bbbbbbbb", "mini-ana"):
            self.assertTrue(registry.update_note(self.project("node")))
        with self.on("m-aaaaaaaa", "mbp-ana"):
            self.assertFalse(registry.update_note(self.project("python")), "this Mac's own part did not change")
        self.assertIn("- **Stack:** python", self.block())
        self.assertIn("- **Stack:** node", self.block())

    def test_a_mac_that_no_longer_has_the_clone_takes_out_only_its_own_part(self):
        with self.on("m-aaaaaaaa", "mbp-ana"):
            registry.update_note(self.project("python"))
        with self.on("m-bbbbbbbb", "mini-ana"):
            registry.update_note(self.project("node"))
        with self.on("m-aaaaaaaa", "mbp-ana"):
            self.assertTrue(registry.update_note(self.project("python", local=False)))
        self.assertNotIn("python", self.block())
        self.assertIn("- **Stack:** node", self.block())

    def test_a_session_starting_or_ending_in_a_checkout_leaves_the_block_as_it_is(self):
        main = {"branch": "feat/export", "active": False, "last_commit_ts": 1763400000, "last_commit": "feat: export"}
        branch = {"name": "feat/export", "where": "local only", "current": True}
        detached = {"branch": None, "path": str(self.root / "wt"), "location": "managed", "active": False,
                    "last_activity": 1763300000}
        with self.on("m-aaaaaaaa", "mbp-ana"):
            self.assertTrue(registry.update_note(self.project("python", main, [detached], [branch])))
            started = self.project("python", dict(main, active=True), [dict(detached, active=True)], [branch])
            self.assertFalse(registry.update_note(started), "a session start changed the note")
        self.assertNotIn("active session", self.block())


    def parts(self, *macs):
        # each Mac (machine id, stack) writes its part of the note's auto block from its own clone
        for mid, stack in macs:
            with self.on(mid, "mac-" + mid[-2:]):
                registry.update_note(self.project(stack))

    def test_the_parts_of_macs_not_seen_for_30_days_go_and_this_macs_own_part_stays(self):
        # gone (seen 31 days ago), live (29 days ago), never in presence, and this Mac, which presence need not name
        self.parts(("m-aaaaaaaa", "python"), ("m-bbbbbbbb", "node"), ("m-cccccccc", "go"), ("m-dddddddd", "rust"))
        now = time.time()
        seen = {"m-bbbbbbbb": now - 31 * DAY, "m-cccccccc": now - 29 * DAY}
        with self.on("m-aaaaaaaa", "mbp-ana"):
            self.assertEqual(registry.prune_auto_parts(seen, now, dry_run=True),
                             [(self.note, "m-bbbbbbbb"), (self.note, "m-dddddddd")])
            self.assertIn("- **Stack:** node", self.block(), "a dry run changed the note")
            self.assertEqual(registry.prune_auto_parts(seen, now), [(self.note, "m-bbbbbbbb"), (self.note, "m-dddddddd")])
        block = self.block()
        self.assertEqual(registry._auto_parts(block[len(registry.AUTO_START):]).keys(), {"m-aaaaaaaa", "m-cccccccc"})
        self.assertIn("- **Stack:** python", block)
        self.assertIn("- **Stack:** go", block)
        self.assertIn("## Session log", self.note.read_text())  # the rest of the note stays

    def test_no_part_goes_while_this_mac_has_no_machine_id_of_its_own(self):
        # identity.machine names the Mac by its hostname when its state cannot be written: this Mac's own part could
        # then be any of them
        self.parts(("m-aaaaaaaa", "python"), ("m-bbbbbbbb", "node"))
        with self.on("mbp-ana", "mbp-ana"):
            self.assertEqual(registry.prune_auto_parts({}, time.time()), [])
        self.assertEqual(registry._auto_parts(self.block()[len(registry.AUTO_START):]).keys(),
                         {"m-aaaaaaaa", "m-bbbbbbbb"})

    def test_a_mac_named_by_its_hostname_adds_no_second_part_of_its_own(self):
        # one sync while the state cannot be written, between syncs under the machine id: the part under the
        # hostname was never this Mac's to take out again, and stayed beside its own
        self.parts(("m-aaaaaaaa", "python"))
        with self.on("mbp-ana", "mbp-ana"):
            registry.update_note(self.project("node"))
        self.parts(("m-aaaaaaaa", "python"))
        self.assertEqual(registry._auto_parts(self.block()[len(registry.AUTO_START):]).keys(), {"m-aaaaaaaa"})
        self.assertIn("- **Stack:** python", self.block())


if __name__ == "__main__":
    unittest.main()
