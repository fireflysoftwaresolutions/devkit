# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew coordination rules (devkit.coord) on hand-built run records: which steps a lane holds and the files it
may write, the assignment intents that outlive lanes and the order a free lane claims them in, blocking and
unblocking, the steps a command changed, the invariant check, and when two scope globs can hold the same file.
Run: python3 -m unittest tests.test_coord_kernel"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import coord  # noqa: E402

T = 1_800_000_000.0


def review(s):
    return s.get("activity") == "review"


def step(name, state="pending", owner="main", label="", scope=(), after=(), **kw):
    return dict(name=name, state=state, owner=owner, label=label, scope=list(scope), after=list(after), **kw)


def lane(persona, state="running", label="", **kw):
    return dict(persona=persona, label=label or persona, state=state, binding="exact", parent="main", **kw)


def run_of(plan, agents=None, **kw):
    done = sum(1 for s in plan if s["state"] in coord.DONE)
    return dict(plan=plan, agents=agents or {}, history=[], step=done, **kw)


class HeldAndScopeTests(unittest.TestCase):
    def test_a_lanes_scope_is_the_union_of_its_running_steps_only(self):
        plan = [step("a", "running", "L", "Tank", ["api/**"]), step("b", "blocked", "L", "Tank", ["b/**"]),
                step("c", "unverified", "L", "Tank", ["c/**"]), step("d", "done", "L", "Tank", ["d/**"]),
                step("e", "running", "L", "Tank", ["tests/test_api.py"], activity="review")]
        run = run_of(plan, {"L": lane("Tank", scope=["old/**"])})
        self.assertEqual([s["name"] for s in coord.held(run, "L")], ["a", "e"])
        self.assertEqual(coord.scope(run, "L"), ["api/**", "tests/test_api.py"])  # never the stored lane scope

    def test_a_lane_with_only_blocked_or_closed_steps_is_free_and_holds_no_files(self):
        plan = [step("a", "blocked", "L", "Tank", ["api/**"]), step("b", "unverified", "L", "Tank", ["b/**"])]
        run = run_of(plan, {"L": lane("Tank")})
        self.assertFalse(coord.busy(run, "L", review=review))
        self.assertEqual(coord.scope(run, "L"), [])

    def test_reviews_share_a_lane_and_a_running_build_step_keeps_it_busy(self):
        r1 = step("r1", "running", "L", "Seraph", activity="review")
        r2 = step("r2", "pending", "main", "Seraph", activity="review")
        build = step("x", "pending", "main", "Seraph")
        run = run_of([r1, r2, build], {"L": lane("Seraph")})
        self.assertFalse(coord.busy(run, "L", r2, review=review))
        self.assertTrue(coord.busy(run, "L", build, review=review))
        self.assertFalse(coord.busy(run, "L", review=review))  # it owns no running step that is not a review


class IntentTests(unittest.TestCase):
    def test_a_legacy_queue_entry_reads_as_an_intent(self):
        one = step("a", label="Tank", waits_lane={"lane": "L1", "ts": T})
        exact = step("b", label="Tank: api", assignment_revision=3,
                     waits_lane={"lane": "L2", "ts": T + 1, "session": "s1", "run_started": T, "step": "b",
                                 "revision": 3})
        several = step("c", label="Tank", waits_lane={"lanes": ["L1", "L2"], "ts": T + 2})
        self.assertEqual((coord.intent(one)["lane"], coord.intent(one)["ts"], coord.intent(one)["member"]),
                         ("L1", T, "Tank"))
        self.assertEqual((coord.intent(exact)["lane"], coord.intent(exact)["rev"]), ("L2", 3))
        self.assertEqual((coord.intent(several)["lane"], coord.intent(several)["member"]), (None, "Tank"))

    def test_a_step_assigned_by_role_without_an_entry_is_a_member_wide_intent(self):
        s = step("a", label="Kid", assigned_at=T + 5)
        self.assertEqual({k: coord.intent(s)[k] for k in ("member", "lane", "ts")},
                         {"member": "Kid", "lane": None, "ts": T + 5})
        self.assertIsNone(coord.intent(step("b", label="")))  # the tech lead's own step
        self.assertIsNone(coord.intent(step("c", "running", "L", "Kid")))  # taken already

    def test_set_intent_replaces_the_legacy_entry(self):
        s = step("a", label="Tank", waits_lane={"lane": "L1", "ts": T}, assignment_revision=2)
        coord.set_intent({}, s, "Tank", "L2", T + 9, front=True)
        self.assertNotIn("waits_lane", s)
        self.assertEqual({k: s["intent"][k] for k in ("member", "lane", "rev", "ts", "front")},
                         {"member": "Tank", "lane": "L2", "rev": 2, "ts": T + 9, "front": True})


class ClaimTests(unittest.TestCase):
    def plan(self):
        return [step("old-role", label="Tank", intent={"member": "Tank", "lane": None, "ts": T + 1}),
                step("bound", label="Tank", intent={"member": "Tank", "lane": "L", "ts": T + 5}),
                step("front", label="Tank", intent={"member": "Tank", "lane": "L", "ts": T + 9, "front": True}),
                step("named", label="Tank", intent={"member": "Tank", "lane": None, "ts": T + 20}),
                step("other-lane", label="Tank", intent={"member": "Tank", "lane": "L2", "ts": T}),
                step("not-ready", label="Tank", after=["gate"], intent={"member": "Tank", "lane": None, "ts": T}),
                step("gate", "running", "main"),
                step("trinity", label="Trinity", intent={"member": "Trinity", "lane": None, "ts": T})]

    def test_a_free_lane_claims_named_then_front_then_bound_then_member_wide_oldest_first(self):
        run = run_of(self.plan(), {"L": lane("Tank"), "L2": lane("Tank", "done")})
        order = []
        for named in ("named", None, None, None):
            got = coord.claimable(run, "L", launch_named=named, review=review)
            order.append(got["name"])
            got["state"] = "done"  # the lane finished it
        self.assertEqual(order, ["named", "front", "bound", "old-role"])
        self.assertIsNone(coord.claimable(run, "L", review=review))  # another lane's, another member's, or waiting

    def test_a_launch_cannot_name_another_members_step_into_its_lane(self):
        run = run_of(self.plan(), {"L": lane("Tank")})
        self.assertEqual(coord.claimable(run, "L", launch_named="trinity", review=review)["name"], "front")

    def test_a_busy_lane_or_one_that_cannot_work_claims_nothing(self):
        busy = run_of(self.plan() + [step("work", "running", "L", "Tank")], {"L": lane("Tank")})
        self.assertIsNone(coord.claimable(busy, "L", review=review))
        for flags in ({"state": "done"}, {"over_limit": True}, {"binding": "unbound"}, {"parent": "L9"}):
            run = run_of(self.plan(), {"L": dict(lane("Tank"), **flags)})
            self.assertIsNone(coord.claimable(run, "L", review=review), flags)

    def test_a_resume_with_a_launch_on_its_way_leaves_member_wide_intents_to_that_launch(self):
        run = run_of([step("role", label="Tank", intent={"member": "Tank", "lane": None, "ts": T})],
                     {"L": lane("Tank")})
        self.assertIsNone(coord.claimable(run, "L", member_wide=False, review=review))

    def test_a_claim_runs_the_step_in_the_lane_removes_its_intent_and_records_why(self):
        run = run_of(self.plan(), {"L": lane("Tank")})
        s = coord.claim(run, "L", T + 30, "its lane holds no other step", review=review)
        self.assertEqual((s["name"], s["state"], s["owner"], s["started"], "intent" in s),
                         ("front", "running", "L", T + 30, False))
        self.assertEqual(run["history"][-1], {"ts": round(T + 30), "step": "front", "from": "pending",
                                              "to": "running", "by": "devkit", "why": "its lane holds no other step"})
        self.assertIsNone(coord.claim(run_of([], {"L": lane("Tank")}), "L", T, "x", review=review))


class BlockTests(unittest.TestCase):
    def test_block_keeps_the_owner_and_frees_the_lane(self):
        s = step("a", "running", "L", "Link", ["docs/**"])
        run = run_of([s], {"L": lane("Link")})
        coord.block(run, s, "D3", T, "waits for the developer")
        self.assertEqual((s["state"], s["owner"], s["waiting"]), ("blocked", "L", "D3"))
        self.assertEqual((coord.busy(run, "L", review=review), coord.scope(run, "L")), (False, []))
        self.assertEqual(run["history"][-1]["to"], "blocked")

    def test_an_answer_runs_the_step_again_in_its_free_lane(self):
        s = step("a", "blocked", "L", "Link", waiting="D3")
        run = run_of([s], {"L": lane("Link")})
        self.assertEqual(coord.unblock(run, s, True, T, review=review), "running")
        self.assertEqual((s["state"], s["owner"], "waiting" in s), ("running", "L", False))

    def test_an_answer_while_the_lane_is_busy_puts_the_step_first_in_that_lanes_queue(self):
        s = step("a", "blocked", "L", "Link", waiting="D3")
        run = run_of([s, step("b", "running", "L", "Link")], {"L": lane("Link")})
        self.assertEqual(coord.unblock(run, s, True, T, review=review), "pending")
        self.assertEqual((s["owner"], coord.intent(s)["lane"], coord.intent(s)["front"]), ("main", "L", True))

    def test_an_answer_after_the_lane_ended_goes_to_the_members_next_lane_not_the_tech_lead(self):
        s = step("a", "blocked", "L", "Link: copy release", waiting="D3")
        run = run_of([s], {"L": lane("Link", "done")})
        self.assertEqual(coord.unblock(run, s, False, T, review=review), "pending")
        self.assertEqual((coord.intent(s)["member"], coord.intent(s)["lane"], coord.intent(s)["front"]),
                         ("Link", None, True))


class ChangesTests(unittest.TestCase):
    def test_changes_lists_exactly_the_steps_whose_state_moved(self):
        run = run_of([step("a", "done"), step("b", "running"), step("c")])
        self.assertEqual(coord.changes({"a": "running", "b": "running"}, run),
                         [("a", "running", "done"), ("c", None, "pending")])
        self.assertEqual(coord.changes(coord.states(run), run), [])


class CheckTests(unittest.TestCase):
    def sound(self):
        return run_of([step("api", "running", "L", "Tank", ["api/**"]),
                       step("ui", "running", "M", "Trinity", ["ui/**"]),
                       step("doc", "done", "L", "Tank")],
                      {"L": lane("Tank"), "M": lane("Trinity")})

    def ids(self, run):
        return {v.split(":")[0] for v in coord.check(run, T, review=review)}

    def test_a_sound_run_breaks_no_invariant(self):
        self.assertEqual(coord.check(self.sound(), T, review=review), [])

    def test_each_broken_invariant_is_named(self):
        cases = {
            "I-owner": lambda r: r["agents"]["L"].update(state="done"),
            "I-one-step": lambda r: r["plan"].append(step("api2", "running", "L", "Tank")),
            "I-scope": lambda r: r["agents"]["L"].update(scope=["doc/**"]),  # a closed step's files
            "I-separate": lambda r: r["plan"][1].update(scope=["api/x.py"]),
            "I-intent": lambda r: r["plan"][0].update(intent={"member": "Tank", "lane": None, "ts": T}),
            "I-claim": lambda r: (r["plan"][0].update(state="done"),
                                  r["plan"].append(step("next", label="Tank", assigned_at=T))),
            "I-history": lambda r: r.update(step=0),
        }
        for rule, breaks in cases.items():
            run = self.sound()
            breaks(run)
            self.assertIn(rule, self.ids(run), rule)

    def test_a_steps_latest_history_entry_must_name_its_state(self):
        run = self.sound()
        run["history"] = [{"ts": T, "step": "api", "from": "pending", "to": "blocked", "by": "main", "why": "x"}]
        self.assertEqual(self.ids(run), {"I-history"})


class OverlapTests(unittest.TestCase):
    def test_a_file_name_is_not_a_folder_another_glob_could_reach_into(self):
        self.assertFalse(coord.globs_overlap("docs/**/bug*.md", "docs/crew.md"))
        self.assertFalse(coord.globs_overlap("docs/agents.md", "docs/**/bug*.md"))
        self.assertTrue(coord.globs_overlap("docs/**/bug*.md", "docs/bug1.md"))

    def test_folders_still_hold_what_lies_under_them(self):
        for a, b in (("docs/", "docs/crew.md"), ("lib/devkit", "lib/devkit/progress.py"),
                     ("src/*", "src/studio/host.py"), ("docs/**", "docs/a/b.md")):
            self.assertTrue(coord.globs_overlap(a, b) and coord.globs_overlap(b, a), (a, b))

    def test_the_checkout_decides_whether_a_literal_name_is_a_folder(self):
        kinds = {"conf.d": "dir", "bin/dev": "file"}
        self.assertTrue(coord.globs_overlap("conf.d", "conf.d/x.conf", kind=kinds.get))
        self.assertFalse(coord.globs_overlap("bin/dev", "bin/dev/x", kind=kinds.get))
        self.assertFalse(coord.globs_overlap("conf.d", "conf.d/x.conf"))  # no checkout at hand: a suffix is a file


if __name__ == "__main__":
    unittest.main()
