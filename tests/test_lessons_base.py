# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The lessons tests' shared fixture: a temporary handoff repository and devkit state for each test, a fixed actor, a
lesson writer with a valid capture shape, and the lesson checks (lessonlint) ready to patch."""
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, lessons, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
DAY = 86400


def lint_module():
    """devkit.lessonlint; while it is not built yet, a stand-in whose checks pass, so the wiring can be tested."""
    try:
        from devkit import lessonlint
        return lessonlint
    except ImportError:
        stand_in = sys.modules.get("devkit.lessonlint")
        if stand_in is None:
            stand_in = types.ModuleType("devkit.lessonlint")
            stand_in.check_anchors = lambda anchors: []
            stand_in.check_shape = lambda title, happened, evidence: []
            stand_in.screen = lambda fields: ""
            sys.modules["devkit.lessonlint"] = stand_in
        return stand_in


class LessonCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        handoff, state = self.root / "handoff", self.root / "state"
        for obj, name, value in ((lessons, "LESSONS", handoff / "lessons"), (lessons, "HANDOFF", handoff),
                                 (lessons, "STATE", state), (lessons, "USAGE_LOCAL", state / "lessons-usage.json"),
                                 (work, "HANDOFF", handoff), (work, "ITEMS", handoff / "items"),
                                 (core, "HANDOFF", handoff)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        ai_off = mock.patch.dict(core.CFG, {"ai": {"enabled": False}})  # never a real model call in tests
        ai_off.start()
        self.addCleanup(ai_off.stop)
        self.lint = lint_module()

    def add(self, title, scope="acme/api", **kw):
        kw.setdefault("happened", "it happened in " + title)
        kw.setdefault("cause", "the cause of " + title)
        kw.setdefault("prevention", "the prevention of " + title)
        return lessons.add(scope, title, a=kw.pop("a", A), **kw)

    def backdate(self, les, days: float):
        """Move a lesson's first and last sighting `days` into the past (as if recorded then)."""
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * DAY))
        path = Path(les["path"])
        text = path.read_text()
        for key in ("first_seen", "last_seen"):
            text = "\n".join(f"{key}: {day}" if ln.startswith(key + ":") else ln for ln in text.split("\n"))
        path.write_text(text)
        return lessons.load(path)

    def ids(self, items):
        return [x["meta"]["id"] for x in items]

    def active_ids(self):
        return sorted(self.ids(lessons.all_lessons()))

    def archived_ids(self):
        return sorted(self.ids(lessons.all_lessons(archived=True)))


def tz(test: unittest.TestCase, zone: str):
    """Run the rest of the test in the time zone `zone`."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()

    def back():
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    test.addCleanup(back)
