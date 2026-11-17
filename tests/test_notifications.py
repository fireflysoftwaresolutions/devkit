# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Notification failures never change the durable message; native builds stay in a sandbox."""
import importlib
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.parsers.expat import ExpatError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.n = importlib.import_module("devkit.notifications")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "DevKit.app"
        binary = self.bundle / "Contents/MacOS/DevKit"
        binary.parent.mkdir(parents=True)
        binary.write_text("isolated native boundary")
        (self.bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleIdentifier": self.n.BUNDLE_ID, "CFBundleDisplayName": "DevKit",
             "CFBundleExecutable": "DevKit", "CFBundleIconFile": "DevKit.icns"}))
        resources = self.bundle / "Contents/Resources"
        resources.mkdir()
        (resources / "DevKit.icns").write_bytes(b"icnsfixture")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.n, "APP", self.bundle).start()
        mock.patch.object(self.n, "ATTEMPTS", self.root / "attempts").start()
        mock.patch.object(self.n.sys, "platform", "darwin").start()
        self.event = {"event_id": "crew:run42:M8", "recipient": "codex:session42",
                      "reference": "message:42:M8", "category": "crew",
                      "body": "SECRET body", "title": "SECRET title", "summary": "SECRET summary"}
        self.original = json.dumps(self.event, sort_keys=True)
        self.message = self.root / "canonical-message.json"
        self.message.write_text(self.original)

    def accepted(self, argv, **kwargs):
        from devkit import state
        self.assertFalse(state._holds(), "Native processes must run outside all record locks")
        payload = json.loads(kwargs["input"])
        self.assertEqual(payload["title"], "DevKit")
        self.assertNotIn("SECRET", kwargs["input"])
        self.assertNotIn("reference", payload)
        self.assertRegex(payload["id"], r"^[0-9a-f]{64}$")
        self.assertEqual(argv, [str(self.bundle / "Contents/MacOS/DevKit"), "--notify"])
        return subprocess.CompletedProcess(argv, 0)

    def test_a_security_notice_reaches_the_helper_as_security_never_as_progress(self):
        from devkit import progress
        categories = []

        def accepted(argv, **kwargs):
            categories.append(json.loads(kwargs["input"])["category"])
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(self.n.subprocess, "run", side_effect=accepted):
            progress.notify("New extension accepted", "Not you? dev audit --action ext.accept", security=True)
        self.assertEqual(categories, ["security"])

    def test_metadata_only_deduplicates_event_and_recipient(self):
        with mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            self.assertTrue(self.n.notify_event(self.event))
            self.assertTrue(self.n.notify_event(self.event))
            self.assertTrue(self.n.notify_event(dict(self.event, recipient="other")))
        self.assertEqual(native.call_count, 2)
        self.assertEqual(self.message.read_text(), self.original)
        self.assertEqual(json.dumps(self.event, sort_keys=True), self.original)

    def test_crew_message_reaches_native_helper_with_crew_category(self):
        from devkit import progress
        with mock.patch.object(progress, "cfg", return_value={"notify": True}), \
                mock.patch.dict(progress.os.environ, {"DEVKIT_NO_NOTIFY": ""}), \
                mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            progress._notify_message("codex", "session42", {"ref": "M8@r-run42", "to": "main"})
        self.assertEqual(native.call_count, 1)
        self.assertEqual(json.loads(native.call_args.kwargs["input"])["category"], "crew")
        attempts = list((self.root / "attempts").glob("*.json"))
        self.assertEqual(len(attempts), 1)
        self.assertTrue(json.loads(attempts[0].read_text())["accepted"])

    def test_crew_alerts_only_for_the_tech_lead_while_no_wait_is_armed(self):
        # agent-to-agent chatter never alerts the developer; an armed wait already wakes the main session
        import os
        from devkit import progress, watch
        with mock.patch.object(progress, "cfg", return_value={"notify": True}), \
                mock.patch.object(watch, "LIVE", self.root / "live"), \
                mock.patch.dict(progress.os.environ, {"DEVKIT_NO_NOTIFY": ""}), \
                mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            progress._notify_message("codex", "session42", {"ref": "M1@r-run42", "to": "a_tank"})
            self.assertEqual(native.call_count, 0)
            watch.register("codex", "session42", "main", os.getpid(), 10)
            progress._notify_message("codex", "session42", {"ref": "M2@r-run42", "to": "main"})
            self.assertEqual(native.call_count, 0)
            progress._notify_message("codex", "session43", {"ref": "M3@r-run43", "to": "main"})
            self.assertEqual(native.call_count, 1)
        # one alert attempt was recorded, for the message to the tech lead with no wait armed
        attempts = [json.loads(p.read_text()) for p in (self.root / "attempts").glob("*.json")]
        self.assertEqual([a["accepted"] for a in attempts], [True])

    def test_peer_message_reaches_native_helper_with_peer_category(self):
        from devkit import peers, progress
        from tests.test_peers import PeerCase, CLAUDE_A, sender
        real_notify = progress.notify
        fixture = PeerCase("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, recipient, _ = fixture.three_sessions()
        with mock.patch.object(progress, "notify", real_notify), \
                mock.patch.object(peers, "_prime", return_value=None), \
                mock.patch.object(progress, "cfg", return_value={"notify": True}), \
                mock.patch.dict(progress.os.environ, {"DEVKIT_NO_NOTIFY": ""}), \
                mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            result = peers.send(sender(CLAUDE_A), "@0199a213-81", "SECRET body", now=fixture.now)
        self.assertEqual(result["notified"], [recipient])
        alerts = [call for call in native.call_args_list if call.args[0][-1] == "--notify"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(json.loads(alerts[0].kwargs["input"])["category"], "peer")

    def test_unknown_category_keeps_generic_progress_fallback(self):
        with mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            self.assertTrue(self.n.notify_event(dict(self.event, category="unknown")))
        self.assertEqual(json.loads(native.call_args.kwargs["input"])["category"], "progress")

    def test_denial_failure_and_timeout_preserve_message_and_allow_retry(self):
        failures = [subprocess.CompletedProcess([], 2), OSError("permission denied"),
                    subprocess.TimeoutExpired("DevKit", 5)]
        for index, failure in enumerate(failures):
            event = dict(self.event, event_id=str(index))
            with self.subTest(failure=str(failure)):
                with mock.patch.object(self.n.subprocess, "run") as native:
                    native.side_effect = failure if isinstance(failure, Exception) else None
                    native.return_value = failure
                    self.assertFalse(self.n.notify_event(event))
                with mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted):
                    self.assertTrue(self.n.notify_event(event))
                self.assertEqual(self.message.read_text(), self.original)

    def test_missing_bundle_never_runs_generic_fallback(self):
        (self.bundle / "Contents/MacOS/DevKit").unlink()
        with mock.patch.object(self.n.subprocess, "run") as native:
            self.assertFalse(self.n.notify_event(self.event))
        native.assert_not_called()
        self.assertEqual(self.message.read_text(), self.original)

    def test_busy_attempt_is_retryable_after_lease(self):
        with mock.patch.object(self.n.subprocess, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.n.notify_event(self.event)
        with mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted) as native:
            self.assertFalse(self.n.notify_event(self.event))
            with mock.patch.object(self.n.time, "time", return_value=10**12):
                self.assertTrue(self.n.notify_event(self.event))
        self.assertEqual(native.call_count, 1)
        self.assertEqual(self.message.read_text(), self.original)

    def test_symlink_attempt_store_is_not_followed(self):
        victim = self.root / "victim"
        victim.mkdir()
        (self.root / "attempts").symlink_to(victim, target_is_directory=True)
        with mock.patch.object(self.n.subprocess, "run") as native:
            self.assertFalse(self.n.notify_event(self.event))
        self.assertEqual(list(victim.iterdir()), [])
        native.assert_not_called()

    def test_missing_icon_never_emits_generic_identity(self):
        (self.bundle / "Contents/Resources/DevKit.icns").unlink()
        with mock.patch.object(self.n.subprocess, "run") as native:
            self.assertFalse(self.n.notify_event(self.event))
        native.assert_not_called()
        self.assertEqual(self.message.read_text(), self.original)

    def test_legacy_notice_never_creates_dedupe_ledger_or_leaks_text(self):
        with mock.patch.object(self.n.subprocess, "run", side_effect=self.accepted):
            self.assertTrue(self.n.notify("SECRET title", "SECRET body", "SECRET subtitle"))
        self.assertFalse((self.root / "attempts").exists())

    def test_malformed_bundle_metadata_is_retryable_without_native_call(self):
        (self.bundle / "Contents/Info.plist").write_bytes(plistlib.dumps([]))
        with mock.patch.object(self.n.subprocess, "run") as native:
            self.assertFalse(self.n.notify_event(self.event))
        native.assert_not_called()
        self.assertEqual(self.message.read_text(), self.original)

    def test_invalid_identity_writes_nothing(self):
        for event in ({}, dict(self.event, event_id=""), dict(self.event, recipient=[])):
            self.assertFalse(self.n.notify_event(event))
        self.assertFalse((self.root / "attempts").exists())

    def test_damaged_xml_returns_failure_without_native_or_message_changes(self):
        (self.bundle / "Contents/Info.plist").write_bytes(b'<?xml version="1.0"?><plist><dict>')
        for api in ("event", "legacy"):
            with self.subTest(api=api), mock.patch.object(self.n.subprocess, "run") as native:
                try:
                    result = (self.n.notify_event(self.event) if api == "event"
                              else self.n.notify("SECRET title", "SECRET body"))
                except ExpatError as error:
                    self.fail(f"Damaged plist escaped the {api} notification API: {error}")
                self.assertFalse(result)
                native.assert_not_called()
                self.assertFalse((self.root / "attempts").exists())
                self.assertEqual(self.message.read_text(), self.original)
                self.assertEqual(json.dumps(self.event, sort_keys=True), self.original)


@unittest.skipUnless(sys.platform == "darwin", "Native bundle validation requires Apple's macOS toolchain")
class NativeBundleTests(unittest.TestCase):
    def test_real_bundle_identity_icon_signature_and_idempotent_reinstall(self):
        import plistlib
        from devkit import notifications as n
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "DevKit.app"
            source = Path(__file__).resolve().parents[1]
            self.assertEqual(n.build_bundle(source, app), app)
            binary = app / "Contents/MacOS/DevKit"
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            self.assertEqual(info["CFBundleDisplayName"], "DevKit")
            self.assertEqual(info["CFBundleIdentifier"], n.BUNDLE_ID)
            self.assertTrue(info["LSUIElement"])
            self.assertNotIn("CFBundleURLTypes", info)
            self.assertEqual((app / "Contents/Resources/DevKit.icns").read_bytes()[:4], b"icns")
            identity = subprocess.run([str(binary), "--check"], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(identity.stdout), {"identifier": n.BUNDLE_ID, "name": "DevKit", "icon": True})
            original = binary.stat().st_mtime_ns
            self.assertEqual(n.build_bundle(source, app), app)
            self.assertEqual(binary.stat().st_mtime_ns, original)
            # A failed rebuild leaves the verified previous executable intact.
            (app / "Contents/Resources/source.sha256").write_text("changed")
            with mock.patch.object(n.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(RuntimeError, "dev install --only cli"):
                    n.build_bundle(source, app)
            self.assertEqual(binary.stat().st_mtime_ns, original)
