# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordination invariants (devkit.coord.check) over the QA engineer's model hunt: the same seeded sequences of
what the tech lead and the crew do, on Claude Code and Codex (tests/test_coordination_model.py), each checked
against every invariant after every action, and each broken invariant reported with the shortest sequence that still
breaks it. DEVKIT_MODEL_SEED replays one seed; DEVKIT_MODEL_SEEDS sets how many sequences run.
Run: python3 -m unittest tests.test_coord_model"""
import os
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_coordination_model as model  # noqa: E402
from devkit import coord  # noqa: E402
from devkit import progress as pg  # noqa: E402

RULES = {
    "I-owner": "every running step belongs to the tech lead or to one running lane",
    "I-one-step": "a lane runs at most one step that is not a review",
    "I-scope": "a lane's scope is that of the steps it runs, never a stored copy",
    "I-separate": "two running lanes never hold the same file",
    "I-intent": "only a pending step carries an intent",
    "I-claim": "a free running lane never leaves a step waiting for it",
    "I-history": "the run's count of closed steps and each step's last history entry match the plan",
}


class CoordWorld(model.World):
    """The QA engineer's world, checked against coord.check after each action as well as its own rules."""

    def check(self, a, before, expect):
        super().check(a, before, expect)
        for v in coord.check(self.run_(), time.time(), review=pg.is_review_step):
            self.flag(v.split(":", 1)[0], v)


class CoordModelTests(unittest.TestCase):
    found = None

    @classmethod
    def hunt(cls):
        if cls.found is None:
            first = {}
            with mock.patch.object(model, "World", CoordWorld):
                one = os.environ.get("DEVKIT_MODEL_SEED")
                seeds = [int(one)] if one else range(int(os.environ.get("DEVKIT_MODEL_SEEDS") or 24))
                for n in seeds:
                    host = "codex" if n % 2 else "claude"
                    seq, broken = model.generate(random.Random(model.SEED + n), host)
                    for rule in broken:
                        if rule in RULES:
                            first.setdefault(rule, (host, seq, n))
                cls.found = {rule: (host, n) + model.shrink(host, seq, rule)
                             for rule, (host, seq, n) in sorted(first.items())}
        return cls.found

    def holds(self, rule):
        got = self.hunt().get(rule)
        if got:
            host, n, seq, text = got
            self.fail(f"{RULES[rule]}: broken on {host} (seed {n}) by {model.show(seq)}\n  → {text}")

    def test_every_running_step_has_a_running_owner(self):
        self.holds("I-owner")

    def test_a_lane_runs_one_step_besides_reviews(self):
        self.holds("I-one-step")

    def test_a_lanes_scope_comes_from_its_running_steps(self):
        self.holds("I-scope")

    def test_two_running_lanes_never_hold_one_file(self):
        self.holds("I-separate")

    def test_only_pending_steps_carry_intents(self):
        self.holds("I-intent")

    def test_no_free_lane_leaves_its_step_waiting(self):
        self.holds("I-claim")

    def test_the_history_matches_the_plan(self):
        self.holds("I-history")


if __name__ == "__main__":
    unittest.main()
