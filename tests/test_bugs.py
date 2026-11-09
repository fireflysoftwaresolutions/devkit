# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bug registry's local core: fingerprints of devkit's own failures, the devkit stack frames they are built from,
the occurrences recorded on this machine, and the marker line that names one."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import bugs, core, work  # noqa: E402

IDENT = ["-c", "user.email=t@example.com", "-c", "user.name=t"]


class StateCase(unittest.TestCase):
    """A temporary state folder for the registry's local files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = (bugs.BUGS, bugs.CRASHES, bugs.SOURCE)
        bugs.BUGS, bugs.CRASHES = self.root / "bugs", self.root / "crashes"

    def tearDown(self):
        bugs.BUGS, bugs.CRASHES, bugs.SOURCE = self.saved
        self.tmp.cleanup()


def _raise_from(source: str, path: Path, call: str):
    """Run `call` from code compiled as if it lived at `path`, -> the exception it raised."""
    ns = {}
    exec(compile(source, str(path), "exec"), ns)
    try:
        ns[call]()
    except Exception as e:  # the exception is what the test inspects
        return e
    raise AssertionError(f"{call} did not raise")


class FingerprintTests(StateCase):
    def test_fingerprint_is_stable_and_short(self):
        crash = bugs.fingerprint("crash", exc_class="KeyError", frames=("work.actor_str",))
        # worked out by hand from the published formula: every machine and version must get these same values
        self.assertEqual(crash, "267a5d8b9826")
        self.assertEqual(bugs.fingerprint("guard", refusal_id="_check_sed", word="sed"), "7539ef480836")
        self.assertNotEqual(bugs.fingerprint("crash", exc_class="ValueError", frames=("work.actor_str",)), crash)

        lib = self.root / "src" / "lib" / "devkit"
        bugs.SOURCE = self.root / "src"
        first = "def outer():\n    return inner()\n\n\ndef inner():\n    raise KeyError('x')\n"
        moved = "\n\n\n# a comment that moves every line\n\n" + first
        with mock.patch.object(bugs, "install_channel", lambda: "release"):
            a, b = (_raise_from(src, lib / "work.py", "outer") for src in (first, moved))
            self.assertNotEqual(_innermost_line(a), _innermost_line(b))  # the line numbers did change
            fa, fb = bugs.devkit_frames(a.__traceback__), bugs.devkit_frames(b.__traceback__)
        self.assertEqual(fa, ("work.inner", "work.outer"))
        self.assertEqual(bugs.fingerprint("crash", exc_class="KeyError", frames=fa),
                         bugs.fingerprint("crash", exc_class="KeyError", frames=fb))


def _innermost_line(exc) -> int:
    tb = exc.__traceback__
    while tb.tb_next:
        tb = tb.tb_next
    return tb.tb_lineno


class FrameTests(StateCase):
    def test_frames_keep_only_devkit_functions(self):
        def helper():  # a function of this test file, outside lib/devkit
            return work.actor_str({})
        with mock.patch.object(bugs, "install_channel", lambda: "release"):
            try:
                helper()
            except KeyError as e:
                frames = bugs.devkit_frames(e.__traceback__)
        self.assertEqual(frames, ("work.actor_str",))

    def test_frames_are_innermost_first_and_at_most_five(self):
        bugs.SOURCE = self.root / "src"
        src = "def walk(n=7):\n    return walk(n - 1) if n else boom()\n\n\ndef boom():\n    raise ValueError()\n"
        with mock.patch.object(bugs, "install_channel", lambda: "release"):
            e = _raise_from(src, self.root / "src" / "lib" / "devkit" / "crew.py", "walk")
            self.assertEqual(bugs.devkit_frames(e.__traceback__), ("crew.boom",) + ("crew.walk",) * 4)

    def test_frames_from_a_checkout_hide_local_functions(self):
        src = self.root / "src"
        lib = src / "lib" / "devkit"
        lib.mkdir(parents=True)
        (src / "VERSION").write_text("26.11.7\n")
        (lib / "work.py").write_text("def shipped():\n    return 1\n")
        git = lambda *args: subprocess.run(["git", *IDENT, "-C", str(src), *args], check=True,  # noqa: E731
                                           capture_output=True)
        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "release")
        git("tag", "v26.11.7")
        # after the release, the developer adds a function of their own and calls it from a shipped one
        local = "def shipped():\n    return added_here()\n\n\ndef added_here():\n    raise KeyError('x')\n"
        (lib / "work.py").write_text(local)
        bugs.SOURCE = src
        self.assertEqual(bugs.install_channel(), "checkout")
        e = _raise_from(local, lib / "work.py", "shipped")
        self.assertEqual(bugs.devkit_frames(e.__traceback__), ("<local>", "work.shipped"))
        git("tag", "-d", "v26.11.7")  # the release's functions are kept per tag: no second git read is needed
        self.assertEqual(bugs.devkit_frames(e.__traceback__), ("<local>", "work.shipped"))

    def test_a_release_install_keeps_every_frame(self):
        pinned = core.pin_root() / "26.11.7-abc1234"
        bugs.SOURCE = pinned
        self.assertEqual(bugs.install_channel(), "release")
        e = _raise_from("def mine():\n    raise KeyError()\n", pinned / "lib" / "devkit" / "work.py", "mine")
        self.assertEqual(bugs.devkit_frames(e.__traceback__), ("work.mine",))


class OccurrenceTests(StateCase):
    def test_occurrence_codes_are_one_time_and_expire(self):
        fp = bugs.fingerprint("crash", exc_class="KeyError", frames=("work.actor_str",))
        occ = bugs.record_occurrence("crash", fp, command="progress step", exc_class="KeyError",
                                     frames=("work.actor_str",), session="s1")
        again = bugs.record_occurrence("crash", fp, session="s1")
        self.assertRegex(occ["code"], r"\A[a-z0-9]{6}\Z")
        self.assertNotEqual(occ["code"], again["code"])  # each occurrence gets a code of its own
        found = bugs.occurrence_for(fp, occ["code"], "s1", now=occ["ts"] + 599)
        self.assertEqual((found["code"], found["fingerprint"], found["command"]), (occ["code"], fp, "progress step"))
        self.assertIsNone(bugs.occurrence_for(fp, "zzzzzz", "s1", now=occ["ts"] + 1))
        self.assertIsNone(bugs.occurrence_for(fp, occ["code"], "s2", now=occ["ts"] + 1))
        self.assertIsNone(bugs.occurrence_for(fp, occ["code"], "s1", now=occ["ts"] + 601))
        lines = (bugs.BUGS / "occurrences.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["code"] for x in lines], [occ["code"], again["code"]])

    def test_install_id_is_created_once_and_private(self):
        first = bugs.install_id()
        self.assertRegex(first, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(bugs.install_id(), first)
        self.assertEqual(stat.S_IMODE(os.stat(bugs.BUGS / "install-id").st_mode), 0o600)


class MarkerTests(unittest.TestCase):
    def test_marker_regex_matches_only_the_exact_line(self):
        text = "devkit bug 3f9a1c2e7b10 in `dev progress step`\ndevkit-bug: 3f9a1c2e7b10 k7q2m9\nafter\n"
        self.assertEqual([m.groups() for m in bugs.MARKER_RE.finditer(text)], [("3f9a1c2e7b10", "k7q2m9")])
        for wrong in (" devkit-bug: 3f9a1c2e7b10 k7q2m9", "devkit-bug: 3f9a1c2e7b10 k7q2m9 x",
                      "xdevkit-bug: 3f9a1c2e7b10 k7q2m9", "devkit-bug: 3F9A1C2E7B10 k7q2m9",
                      "devkit-bug: 3f9a1c2e7b1 k7q2m9", "devkit-bug: 3f9a1c2e7b10 K7Q2M9",
                      "devkit-bug: 3f9a1c2e7b10 k7q2m", "devkit-bug:  3f9a1c2e7b10 k7q2m9"):
            with self.subTest(line=wrong):
                self.assertIsNone(bugs.MARKER_RE.search("before\n" + wrong + "\nafter"))


if __name__ == "__main__":
    unittest.main()
