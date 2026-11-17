# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bounded mailbox qualification with optional timing evidence, never timing assertions."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_progress import TempState
from devkit import messaging, peers, progress, state, watch

TIMING = False


def measured(call, count=10):
    samples = []
    result = None
    for index in range(count + 3):
        started = time.perf_counter()
        result = call()
        elapsed = (time.perf_counter() - started) * 1000
        if index >= 3:
            samples.append(elapsed)
    samples.sort()
    return {"p50_ms": samples[len(samples) // 2], "p95_ms": samples[int(len(samples) * .95)]}, result


class MailboxQualification(unittest.TestCase):
    def test_pending_mail_is_bounded_and_does_not_launch_processes(self):
        evidence = {}
        timing = TIMING
        for name, count, size in (("empty", 0, 0), ("20_previews", 20, 40), ("1MiB_mailbox", 32, 32768)):
            with self.subTest(name=name), TempState():
                root = progress.RUNS.parent
                with patch.object(peers, "PEERS", root / "peers"), patch.object(watch, "LIVE", root / "watch/live"):
                    run = progress.start("Mailbox qualification", plan="check=1m")
                    for index in range(count):
                        progress._post(run, "main", "main", str(index) + " " + "x" * size, time.time())
                    progress._save(run, touch_session=False)
                    before = {str(p): p.read_bytes() for p in (root / "messaging-v2").rglob("*.json")}
                    with patch.object(subprocess, "Popen", side_effect=AssertionError("mailbox spawned a process")):
                        stats, output = measured(lambda: watch.mail("claude", "s1", mark=False), 30 if timing else 1)
                    self.assertLessEqual(len(output.encode()), 6144)
                    self.assertEqual(bool(count), bool(output))
                    self.assertEqual(before, {str(p): p.read_bytes() for p in (root / "messaging-v2").rglob("*.json")})
                    stats["output_bytes"] = len(output.encode())
                    evidence[name] = {"candidate": stats}
        if timing:
            print("MAILBOX_TIMINGS " + json.dumps(evidence, sort_keys=True))

    def test_capacity_rejection_preserves_the_durable_record(self):
        timing = TIMING
        with TempState():
            path = progress.RUNS.parent / "durable.json"
            document = {"body": "x" * (1024 * 1024)}
            stats, _ = measured(lambda: state.replace(path, document, durable=True, max_bytes=messaging.STORE_MAX),
                                10 if timing else 1)
            before = path.read_bytes()
            oversized = {"body": "x" * messaging.STORE_MAX}

            def reject():
                with self.assertRaisesRegex(messaging.MessageError, "capacity"):
                    messaging.check_capacity(oversized)
                self.assertEqual(path.read_bytes(), before)

            rejected, _ = measured(reject, 10 if timing else 1)
            self.assertEqual(state.read(path), document)
            if timing:
                print("DURABILITY_TIMINGS " + json.dumps({"fsync_1MiB": stats, "capacity_rejection": rejected,
                      "baseline_quota": "No equivalent byte quota; legacy retention truncates at 200 messages."}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timings", action="store_true")
    args = parser.parse_args()
    TIMING = args.timings
    unittest.main(argv=[sys.argv[0]], verbosity=2)
