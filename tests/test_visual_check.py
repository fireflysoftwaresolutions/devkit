# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev visual check <VC-…|VA-…> [--only <check,…>]` (lib/devkit/visual/capture_checks.py): the checks run again on a
# capture's recipe, or on the recipe of every capture of an audit, and their signals kept on the capture they ran
# for. A signal found again keeps its number and triage, a new one takes the capture's next number, and an earlier
# one not found again stays as it was. The merge tests need no tool; the record tests take real captures of the
# page of tests/test_visual_store.py, served on 127.0.0.1, in a temporary handoff repository, state folder, and
# configuration (core.HANDOFF, core.STATE, core.CONFIG_FILE). Without node, gitleaks, or the pinned Playwright and
# its Chromium, the record tests skip and say which is missing.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_check
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit.visual import capture_checks, store  # noqa: E402
import test_visual_store as base  # noqa: E402

P = base.P
ALL = ("devkit", "axe", "impeccable")


def signal(n, check, element, *, triage=None, other=None, measured="m", duplicates=(), **extra):
    # a capture's signal as capture.cjs writes it, anchored on one element of its description
    s = {"n": n, "check": check, "pass": "layout", "rule": ["wcag:2.5.8"],
         "anchor": {"element": element, "region": [0, 0, 10, 10], "selector": f"#{element.lower()}", "name": None},
         **extra, "measured": measured, "threshold": "t", "triage": triage, "duplicates": list(duplicates)}
    if other:
        s["other"] = {"element": None, "region": [5, 5, 10, 10], "selector": other, "name": None}
    return s


DISMISSED = {"as": "dismissed", "finding": None, "why": "decorative", "by": "sati", "model": "human",
             "at": "2026-11-18T10:00:00Z"}


class MergeTest(unittest.TestCase):
    def test_a_signal_found_again_keeps_its_number_and_triage_and_takes_the_new_measurement(self):
        stored = [signal(1, "overlap", "E4", triage=DISMISSED, other="#amount", measured="40×30 px intersection")]
        found = [signal(1, "overlap", "E4", other="#amount", measured="40×20 px intersection")]
        merged, counts = capture_checks.merge(stored, found, ran=ALL)
        self.assertEqual([(s["n"], s["triage"], s["measured"]) for s in merged],
                         [(1, DISMISSED, "40×20 px intersection")])
        self.assertEqual(counts, {"again": [1], "new": [], "gone": []})

    def test_a_new_signal_takes_the_number_after_the_captures_highest_and_is_untriaged(self):
        # a changed-region signal that verify numbered on the capture counts, and is never matched
        stored = [signal(1, "overlap", "E4", other="#amount"),
                  {"n": 2, "check": "changed-region", "pass": "layout", "rule": [], "anchor": {"region": [0, 0, 9, 9]},
                   "measured": "81 pixels changed", "triage": DISMISSED, "duplicates": []}]
        found = [signal(1, "overlap", "E4", other="#amount"), signal(2, "tap-target", "E5", triage=DISMISSED)]
        merged, counts = capture_checks.merge(stored, found, ran=ALL)
        self.assertEqual([(s["n"], s["check"], s["triage"]) for s in merged],
                         [(1, "overlap", None), (2, "changed-region", DISMISSED), (3, "tap-target", None)])
        self.assertEqual(counts, {"again": [1], "new": [3], "gone": []})

    def test_an_earlier_signal_not_found_again_stays_as_it_was_and_is_listed(self):
        gone = signal(2, "tap-target", "E5", triage=DISMISSED)
        merged, counts = capture_checks.merge([signal(1, "overlap", "E4", other="#amount"), gone],
                                              [signal(1, "overlap", "E4", other="#amount")], ran=ALL)
        self.assertEqual(merged[1], gone)
        self.assertEqual(counts, {"again": [1], "new": [], "gone": [2]})

    def test_overlaps_of_one_element_with_two_others_are_two_signals(self):
        stored = [signal(1, "overlap", "E4", other="#amount", triage=DISMISSED)]
        found = [signal(1, "overlap", "E4", other="#total"), signal(2, "overlap", "E4", other="#amount")]
        merged, counts = capture_checks.merge(stored, found, ran=ALL)
        self.assertEqual([(s["n"], s["other"]["selector"], s["triage"]) for s in merged],
                         [(1, "#amount", DISMISSED), (2, "#total", None)])
        self.assertEqual(counts, {"again": [1], "new": [2], "gone": []})

    def test_a_signal_folded_into_another_sources_signal_is_found_again(self):
        # R68: axe's target-size kept the overlap as its duplicate; a run that keeps the overlap alone finds it again
        folded = signal(3, "axe", "E4", axe_rule="target-size", triage=DISMISSED,
                        duplicates=[{"check": "overlap", "axe_rule": None, "rule": ["wcag:2.5.8"], "measured": "m",
                                     "threshold": "t", "other": {"selector": "#amount"}}])
        merged, counts = capture_checks.merge([folded], [signal(1, "overlap", "E4", other="#amount")], ran=ALL)
        self.assertEqual([(s["n"], s["check"], s["triage"]) for s in merged], [(3, "overlap", DISMISSED)])
        self.assertEqual(counts, {"again": [3], "new": [], "gone": []})

    def test_only_keeps_the_named_checks_and_leaves_every_other_signal_as_it_was(self):
        tap = signal(2, "tap-target", "E5")
        stored = [signal(1, "overlap", "E4", other="#amount", triage=DISMISSED), tap]
        found = [signal(1, "overlap", "E4", other="#amount"), signal(2, "axe", "E7", axe_rule="color-contrast")]
        merged, counts = capture_checks.merge(stored, found, only={"overlap"}, ran=ALL)
        self.assertEqual([(s["n"], s["check"], s["triage"]) for s in merged],
                         [(1, "overlap", DISMISSED), (2, "tap-target", None)])
        self.assertEqual(merged[1], tap)
        self.assertEqual(counts, {"again": [1], "new": [], "gone": []})

    def test_a_check_that_did_not_run_this_time_lists_none_of_its_signals_as_gone(self):
        stored = [signal(1, "axe", "E7", axe_rule="color-contrast", triage=DISMISSED)]
        merged, counts = capture_checks.merge(stored, [], ran=("devkit",))
        self.assertEqual(merged, stored)
        self.assertEqual(counts, {"again": [], "new": [], "gone": []})

    def test_only_takes_check_ids_and_refuses_another_word_with_the_nearest(self):
        self.assertIsNone(capture_checks.only_checks(None))
        self.assertEqual(capture_checks.only_checks("overlap, axe,tap-target"), {"overlap", "axe", "tap-target"})
        for given, says in (("overlapp", 'not "overlapp". Nearest: overlap.'), (" , ", "Name the checks")):
            with self.subTest(given=given):
                with self.assertRaises(store.VisualError) as e:
                    capture_checks.only_checks(given)
                self.assertIn(says, str(e.exception))


@unittest.skipUnless(base.BROWSERS, base.NO_BROWSER or "")
class RecordTest(base.StoreCase):
    def captures(self):
        return sorted(p.stem for p in (self.records / "captures").glob("*.json"))

    def blobs(self):
        return sorted(p.name for p in self.local.rglob("*") if p.is_file())

    def test_signals_of_a_capture_taken_without_checks_are_numbered_on_that_capture(self):
        c = self.shot(check=False)
        self.assertEqual(c["checks"]["signals"], [])
        held = self.blobs()
        [out] = capture_checks.check(P, c["id"])
        rec = self.record(c["id"])
        sigs = rec["checks"]["signals"]
        self.assertEqual([s["n"] for s in sigs], list(range(1, len(sigs) + 1)))
        self.assertIn(("overlap", "E4"), [(s["check"], s["anchor"]["element"]) for s in sigs])
        self.assertTrue(all(s["triage"] is None for s in sigs))
        self.assertEqual((out["result"], out["new"], out["again"], out["gone"]),
                         ("checked", [s["n"] for s in sigs], [], []))
        self.assertIn("devkit", rec["checks"]["ran"])
        self.assertIn("checks run again", rec["history"][-1]["change"])
        # the run took no capture: no record, no picture, and no description beside the capture's own
        self.assertEqual(self.captures(), [c["id"]])
        self.assertEqual(self.blobs(), held)
        self.assertEqual({k: rec[k] for k in ("image", "description", "taken")},
                         {k: c[k] for k in ("image", "description", "taken")})

    def test_signals_found_again_keep_their_numbers_and_triage(self):
        c = self.shot()
        first = c["checks"]["signals"]
        store.dismiss(P, f"{c['id']}#1", "decorative overlap", model="human")
        triage = self.record(c["id"])["checks"]["signals"][0]["triage"]
        [out] = capture_checks.check(P, c["id"])
        sigs = self.record(c["id"])["checks"]["signals"]
        self.assertEqual([(s["n"], s["check"], s["anchor"]["element"]) for s in sigs],
                         [(s["n"], s["check"], s["anchor"]["element"]) for s in first])
        self.assertEqual(sigs[0]["triage"], triage)
        self.assertEqual((out["again"], out["new"], out["gone"]), ([s["n"] for s in first], [], []))

    def test_only_brings_in_the_named_checks_and_a_full_run_numbers_the_rest_after_them(self):
        c = self.shot(check=False)
        capture_checks.check(P, c["id"], only=capture_checks.only_checks("overlap"))
        rec = self.record(c["id"])
        self.assertEqual([(s["n"], s["check"]) for s in rec["checks"]["signals"]], [(1, "overlap")])
        self.assertEqual(rec["checks"]["ran"], ["devkit"])
        [out] = capture_checks.check(P, c["id"])
        sigs = self.record(c["id"])["checks"]["signals"]
        self.assertEqual(sigs[0]["check"], "overlap")
        self.assertEqual((out["again"], out["new"]), ([1], [s["n"] for s in sigs[1:]]))
        self.assertTrue(out["new"])

    def test_a_page_that_lays_out_differently_is_refused_and_the_capture_is_unchanged(self):
        c = self.shot()
        path = self.records / "captures" / f"{c['id']}.json"
        before = path.read_bytes()
        self.page.write_text(base.PAGE.replace("<body><h1>", "<body><p>A new notice above the title.</p><h1>"))
        with self.assertRaises(store.VisualError) as e:
            capture_checks.check(P, c["id"])
        self.assertIn(f"The page of {c['id']} lays out differently now", str(e.exception))
        self.assertIn(f"Nothing was changed. Capture the page as it is now, with its own signals: dev visual capture "
                      f"--again {c['id']}", str(e.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.captures(), [c["id"]])

    # the refusal names where the description is as every other command does (store.not_here): gone from this Mac's
    # store when this Mac took the capture, else on the Mac that took it
    def test_a_capture_whose_description_is_not_on_this_mac_is_refused(self):
        c = self.shot()
        (self.local / "desc" / f"{c['description']['sha256']}.json.gz").unlink()
        self.patched(base.work, "PRESENCE", new=self.tmp / "presence")
        base.presence(self.tmp / "presence", "bob@m-studio01", "studio")
        path = self.records / "captures" / f"{c['id']}.json"
        then = ("so devkit cannot tell whether the page still lays out as its picture shows. Nothing was changed. "
                f"Capture it again here, with its own signals: dev visual capture --again {c['id']}")
        for where, said in (("m-test0001", "is no longer in this Mac's store"),
                            ("m-studio01", "is not on this Mac: it was taken on another Mac (m-studio01)")):
            with self.subTest(where=where):
                rec = self.record(c["id"])
                rec["image"]["where"] = f"local:{where}"
                store.save(P, rec)
                before = path.read_bytes()
                with self.assertRaises(store.VisualError) as e:
                    capture_checks.check(P, c["id"])
                self.assertEqual(str(e.exception), f"The description of {c['id']} {said}, {then}")
                self.assertEqual(path.read_bytes(), before)

    def test_an_audit_is_checked_capture_by_capture_and_an_external_picture_is_left_out(self):
        c = self.shot(check=False)
        picture = self.tmp / "shot.png"
        picture.write_bytes(base.png(40, 30))
        ext = store.capture(P, str(picture))[0]
        out = capture_checks.check(P, c["audit"])
        self.assertEqual(sorted((o["capture"], o["result"]) for o in out),
                         sorted([(c["id"], "checked"), (ext["id"], "skipped")]))
        self.assertTrue(self.record(c["id"])["checks"]["signals"])
        self.assertEqual(self.record(ext["id"])["checks"]["signals"], [])
        with self.assertRaises(store.VisualError) as e:
            capture_checks.check(P, ext["id"])
        self.assertIn(f"{ext['id']} is an external picture: it has no recipe to run the checks on", str(e.exception))


if __name__ == "__main__":
    unittest.main()
