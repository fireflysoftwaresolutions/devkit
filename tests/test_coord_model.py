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
    "fix-after-changes": "the step after a review that asked for changes does not wait for that review",
    "named-step-claimed": "a launch that names a step waiting for its member is no follow-up of a finished one",
}
# "api docs" shares a word with "api": a launch naming the finished "api" can name the waiting "api docs" too
PLAN = model.PLAN + "; fix=20m [backend] after review; api docs=15m [backend] after api"
STEPS = dict(model.STEPS, fix="tank", **{"api docs": "tank"})
SCOPES = dict(model.SCOPES, fix="fix/**", **{"api docs": "docs/**"})


QA_PROPOSE = model.propose  # the hunt patches model.propose with the one below


def propose(rng, w=None):
    """The QA engineer's proposals, and now and then the reviewer asking for changes on the review it runs, or a
    launch of the Backend engineer whose description names a finished step of its and some other words."""
    if w is not None and rng.random() < 0.15:
        return {"do": "reject", "member": "seraph", "step": "review", "how": "role", "named": True, "clean": True}
    if w is not None and rng.random() < 0.1:
        return {"do": "generic", "member": "tank", "step": "api docs", "how": "role", "named": False, "clean": True}
    return QA_PROPOSE(rng, w)


def show(seq: list) -> str:
    """model.show, with this hunt's own actions."""
    own = {"reject": lambda x: "Seraph asks for changes on review",
           "generic": lambda x: f"launch Tank “Tank: {x.get('used')}”"}
    return " → ".join(f"{i}. {own[x['do']](x)}" if x["do"] in own else
                      f"{i}. " + model.show([x]).split(". ", 1)[1] for i, x in enumerate(seq, 1))


class CoordWorld(model.World):
    """The QA engineer's world, checked against coord.check after each action as well as its own rules."""

    def act(self, a):
        if a["do"] == "generic":
            return self.generic(a)
        if a["do"] != "reject":
            return super().act(a)
        aid = self.live("seraph")
        review = self.plan()["review"]
        if not aid or review.get("owner") != aid or review.get("state") != "running":
            return False
        before = {k: v.get("state") for k, v in self.plan().items()}
        cmd = 'dev progress step "review" --step review --check "✗ changes requested"'
        pg.step("review", which="review", check="✗ changes requested", from_agent=aid, now=self.now())
        self.commands.setdefault(aid, []).append(cmd)
        self.stop(aid, "seraph", self.commands[aid])
        self.stop(aid, "seraph", self.commands[aid], again=True)
        self.ended.add(aid)
        fix = self.plan()["fix"]
        if fix.get("state") == "pending" and "review" in pg.deps_open(self.run_(), fix) \
                and self.plan()["review"].get("state") == "unverified":
            self.flag("fix-after-changes", "“fix” still waits for the review that asked for changes")
        self.check(a, before, None)
        return True

    def generic(self, a):
        """A new launch of the Backend engineer described by a finished step of its and other words (“Tank: api
        release notes”): when the description names a step waiting for its member too, the lane is no follow-up of
        the finished step."""
        member, name = "tank", model.MEMBERS["tank"][0]
        if self.live(member) or (self.lanes.get(member) and self.lanes[member] not in self.ended):
            return False
        plan = self.plan()
        done = [n for n, m in STEPS.items() if m == member and plan[n].get("state") in ("done", "skipped")]
        if not done:
            return False
        before = {k: v.get("state") for k, v in plan.items()}
        self.close_member_call(member)
        task = a["used"] = f"{done[0]} release notes"
        aid = f"a{member}g{self.k}"
        try:
            model.launch(aid, member if self.host == "codex" else f"devkit:{member}", f"{name}: {task}",
                         at=self.now(), agent=self.host)
        except AssertionError:  # Codex's admission refused the launch (a slot, the order): nothing started
            return False
        self.lanes[member] = aid
        self.ended.discard(aid)
        run = self.run_()
        lane = run["agents"].get(aid) or {}
        named = [s["name"] for s in run["plan"] if s.get("state") == "pending" and STEPS.get(s["name"]) == member
                 and (s.get("owner") or "main") == "main" and (s.get("label") or "").lower().startswith(member)
                 and pg._name_score(task, s["name"])[1]]
        if named and lane.get("follow_up"):
            self.flag("named-step-claimed", f"“{name}: {task}” names {', '.join(named)}, which waits for {name}, "
                      f"yet its lane follows up the finished “{lane['follow_up']}” and takes no step")
        self.check(a, before, None)
        return True

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
            with mock.patch.object(model, "World", CoordWorld), mock.patch.object(model, "PLAN", PLAN), \
                    mock.patch.object(model, "STEPS", STEPS), mock.patch.object(model, "SCOPES", SCOPES), \
                    mock.patch.object(model, "propose", propose):
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
            self.fail(f"{RULES[rule]}: broken on {host} (seed {n}) by {show(seq)}\n  → {text}")

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

    def test_the_fix_after_a_review_that_asked_for_changes_can_start(self):
        self.holds("fix-after-changes")

    def test_a_launch_naming_a_waiting_step_is_no_follow_up_of_a_finished_one(self):
        self.holds("named-step-claimed")


if __name__ == "__main__":
    unittest.main()
