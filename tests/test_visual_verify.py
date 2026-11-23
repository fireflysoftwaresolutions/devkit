# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev visual verify` (store.verify, lib/devkit/visual/store.py) captures a finding's recipe again on the working tree,
# finds its anchor again (by locator, then its own test id or id, then its selector spelled one way, then its role
# and name, and only then by its old position, which the outcome says), asks whether the check that raised it still
# fires, compares the pictures with pixelmatch (compare.compare_captures), and writes the finding's verification and
# its new status; the changed regions outside the anchor become signals numbered on the new capture. Each test runs in a
# temporary handoff repository, state folder, and configuration (tests/test_visual_store.py's StoreCase patches
# core.HANDOFF, core.STATE, and core.CONFIG_FILE), and captures a page it serves on 127.0.0.1 in the pinned Chromium:
# the page plants Export overlapping Amount, and each test rewrites it to the state it needs. Without node, gitleaks,
# or the pinned Playwright and its Chromium, the tests skip and say which is missing.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_verify
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import cli  # noqa: E402
from devkit.visual import commands, compare, store  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
import test_visual_store as base  # noqa: E402

P = base.P
AMOUNT = "#amount { position: absolute; left: 100px; top: 20px; width: 120px; height: 40px; }"
# Amount moved clear of Export: the overlap is gone, both buttons stay 40 px high (tap-target still fires on each)
FIXED = base.PAGE.replace(AMOUNT, "#amount { position: absolute; left: 160px; top: 10px; width: 120px; height: 40px; }")
# the same fix with Amount 48 px high, so its tap-target advice no longer fires either
FIXED_TALL = base.PAGE.replace(AMOUNT, "#amount { position: absolute; left: 160px; top: 10px; width: 120px; "
                                       "height: 48px; }")
# the fix, and the heading drawn 20 px lower without moving anything else on the page
HEADING = "h1 { margin: 16px; font-size: 28px; }"
FIXED_AND_HEADING = FIXED.replace(HEADING, "h1 { margin: 16px; font-size: 28px; position: relative; top: 20px; }")
# Export moved left with another class: still over Amount, so the overlap still fires on it
MOVED_EXPORT = base.PAGE.replace("#export { position: absolute; left: 20px;", ".renamed { position: absolute; left: 4px;") \
    .replace('<button id="export" data-testid="export">', '<button class="renamed" data-testid="export">')
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# a header whose lead paragraph is 13 px, with a 14 px meta line under it; a second paragraph in main makes the
# description's selector for the lead `header > p`, while the checks write it from the root, `html > body > header > p`
LEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Report</title>
<style>body { margin: 0; font: 16px Arial, sans-serif; } header, main { padding: 0 16px; }
h1 { margin: 0; font-size: 28px; line-height: 48px; } p, li { margin: 0; line-height: 40px; }
ul { margin: 0; padding: 0; list-style: none; } .lead { font-size: 13px; } li { font-size: 14px; }</style></head>
<body><header><h1>Report</h1><p class="lead">Three signals wait for triage.</p><ul><li>Fourteen captures</li></ul>
</header><main><p>The body copy is 16 px.</p></main></body></html>
"""
LEAD_LINE = '<p class="lead">Three signals wait for triage.</p>'
META_LINE = "<ul><li>Fourteen captures</li></ul>"
# the fix: the lead is 16 px and moves under the meta line, which (still 14 px, by design) now sits where it was
LEAD_FIXED = LEAD.replace(".lead { font-size: 13px; }", ".lead { font-size: 16px; }") \
    .replace(LEAD_LINE + META_LINE, META_LINE + LEAD_LINE)
# the lead removed: the meta line moves up into its old position
LEAD_GONE = LEAD.replace(LEAD_LINE, "")
# the lead and the meta line removed: nothing is left where the lead was
LEAD_EMPTY = LEAD_GONE.replace(META_LINE, "")


@unittest.skipUnless(base.BROWSERS, base.NO_BROWSER or "")
class VerifyCase(base.StoreCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(commands, "_project", lambda a: getattr(a, "project", None) or P)
        p.start()
        self.addCleanup(p.stop)
        # a server of this test's own for the page, so a test can stop it: the app goes away
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(base.Quiet, directory=str(self.page.parent)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/invoices.html"

    def server_stop(self):
        self.server.shutdown()
        self.server.server_close()

    def signal(self, capture, check, element=None):
        found = [s for s in capture["checks"]["signals"] if s["check"] == check and
                 (element is None or s["anchor"]["element"] == element)]
        self.assertTrue(found, f"no {check} signal on {capture['id']}: {capture['checks']['signals']}")
        return found[0]

    def from_signal(self, capture, check, element=None, **kw):
        s = self.signal(capture, check, element)
        return store.add_finding(P, from_signal=f"{capture['id']}#{s['n']}", pass_=kw.pop("pass_", "layout"),
                                 severity=kw.pop("severity", "P1"), title=kw.pop("title", f"{check} on the bar"),
                                 impact="A clerk taps the wrong button.", fix="Space the buttons.", **kw)

    def fixed(self, finding):
        return store.set_finding(P, finding["id"], status="fixed", why="the buttons are spaced", commit="4f2a9c1")

    def element_of(self, capture, selector):
        return next(e for e in self.description(capture)["elements"] if e["selector"] == selector)

    def dev(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            a = cli.parse(["visual", "-p", P, *argv])
            rc = a.fn(a)
        return rc, ANSI.sub("", out.getvalue()), ANSI.sub("", err.getvalue())


class CheckFindingTest(VerifyCase):
    def test_vd_ac_20_a_fixed_overlap_is_verified_on_a_new_capture_of_the_same_recipe(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("pass", "verified"))
        v = doc["verification"]
        after = self.record(v["capture"])
        self.assertEqual(after["again_of"], before["id"])
        self.assertEqual(after["recipe"], before["recipe"])
        self.assertNotIn("overlap", [s["check"] for s in after["checks"]["signals"]])
        self.assertEqual((v["check"], v["accepted_by"], v["error"]), ("pass", None, None))
        self.assertGreater(v["inside"], 0)
        self.assertEqual(doc["history"][-1]["change"], "verified")
        self.assertEqual(doc["history"][-1]["by"], "devuser")

    def test_the_difference_picture_is_kept_in_the_store_under_the_hash_the_verification_names(self):
        # spec v28: verification.diff holds the SHA-256, size, and scale of pixelmatch's difference picture
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(FIXED)
        store.verify(P, f["id"])
        diff = self.record(f["id"])["verification"]["diff"]
        blob = (self.local / "blobs" / f"{diff['sha256']}.png").read_bytes()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), diff["sha256"])
        self.assertEqual((diff["width"], diff["height"], diff["scale"]),
                         (before["image"]["width"], before["image"]["height"], 1))
        self.assertEqual(blob[16:24], diff["width"].to_bytes(4, "big") + diff["height"].to_bytes(4, "big"))

    def test_vd_ac_20_an_unchanged_page_reopens_the_fixed_finding_with_the_measured_overlap(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["check"]), ("fail", "reopened", "fail"))
        measured = self.signal(before, "overlap")["measured"]
        # the line and the history name the check; what it measured, which can hold page text, stays in the record
        after = doc["verification"]["capture"]
        self.assertEqual(doc["verification"]["measured"], measured)
        self.assertEqual((doc["history"][-1]["change"], doc["history"][-1]["why"]),
                         ("reopened", f"still there on {after}: overlap"))
        self.assertEqual(out["message"], f"{f['id']} is still there: overlap fires again on {after}. Status: reopened.")

    def test_a_finding_not_marked_fixed_stays_open_when_the_check_still_fires(self):
        before = self.shot()
        f = self.from_signal(before, "overlap")
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["check"]), ("fail", "open", "fail"))

    def test_vd_ac_21_a_change_outside_the_anchor_becomes_a_changed_region_signal_on_the_new_capture(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(FIXED_AND_HEADING)
        store.verify(P, f["id"])
        v = self.record(f["id"])["verification"]
        after = self.record(v["capture"])
        regions = [s for s in after["checks"]["signals"] if s["check"] == "changed-region"]
        self.assertTrue(regions, after["checks"]["signals"])
        self.assertEqual(v["new_signals"], [f"{after['id']}#{s['n']}" for s in regions])
        checked = [s["n"] for s in after["checks"]["signals"] if s["check"] != "changed-region"]
        self.assertEqual([s["n"] for s in regions], list(range(len(checked) + 1, len(checked) + len(regions) + 1)))
        h1 = next(e for e in self.description(after)["elements"] if e["role"] == "heading")
        x, y, w, h = regions[0]["anchor"]["region"]
        self.assertTrue(x < h1["box"][0] + h1["box"][2] and h1["box"][0] < x + w and y < h1["box"][1] + h1["box"][3]
                        and h1["box"][1] < y + h, (regions[0], h1["box"]))
        self.assertEqual((regions[0]["triage"], regions[0]["duplicates"]), (None, []))
        self.assertIn(v["new_signals"][0], [ref for ref, _s, _o in store.signals(P, after["id"], untriaged=True)])

    def test_vd_ac_66_the_anchor_is_found_again_by_its_locator_after_its_class_and_box_change(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(MOVED_EXPORT)
        store.verify(P, f["id"])
        doc = self.record(f["id"])
        after = self.record(doc["verification"]["capture"])
        export = self.element_of(after, '[data-testid="export"]')
        self.assertEqual(doc["verification"]["anchor"]["found_by"], "locator")
        self.assertEqual(doc["verification"]["anchor"]["region"], export["box"])
        self.assertNotEqual(export["box"], f["anchor"]["region"])
        self.assertEqual(doc["status"], "reopened")

    def test_vd_ac_23_an_experiment_capture_is_never_the_after_picture(self):
        store.start(P, "screen", "Q?", viewports=["390x844"])
        before = store.capture(P, self.url, state="list", css="h1 { color: #444 }")[0]
        self.assertTrue(before["experiment"])
        f = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(FIXED)
        store.verify(P, f["id"])
        after = self.record(self.record(f["id"])["verification"]["capture"])
        self.assertFalse(after["experiment"])
        self.assertIsNone(after["recipe"]["css"])
        self.assertEqual(self.record(f["id"])["status"], "verified")

    def test_a_recapture_that_fails_records_the_error_and_keeps_the_status(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.server_stop()
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("error", "fixed"))
        v = doc["verification"]
        self.assertIsNone(v["capture"])
        self.assertEqual(v["check"], "none")
        self.assertRegex(v["error"], r"did not load")
        self.assertNotRegex(v["error"], r"Nothing was recorded")
        self.assertEqual(doc["history"][-1]["change"], "verify failed")
        self.assertEqual(len(list((self.records / "captures").glob("*.json"))), 1)

    def test_vd_ac_24_the_findings_this_one_is_the_root_of_are_checked_on_the_same_new_capture(self):
        before = self.shot()
        root = self.fixed(self.from_signal(before, "overlap"))
        export, amount = (self.element_of(before, s)["id"] for s in ('[data-testid="export"]', "#amount"))
        small_amount = self.fixed(self.from_signal(before, "tap-target", amount, pass_="accessibility",
                                                   severity="P3", root=root["id"]))
        small_export = self.fixed(self.from_signal(before, "tap-target", export, pass_="accessibility",
                                                   severity="P3", root=root["id"]))
        self.page.write_text(FIXED_TALL)
        outs = store.verify(P, root["id"])
        self.assertEqual(outs[0]["finding"], root["id"])
        self.assertEqual(sorted(o["finding"] for o in outs[1:]), sorted([small_amount["id"], small_export["id"]]))
        docs = [self.record(x["id"]) for x in (root, small_amount, small_export)]
        self.assertEqual([d["status"] for d in docs], ["verified", "verified", "reopened"])
        self.assertEqual(len({d["verification"]["capture"] for d in docs}), 1)
        self.assertEqual(len(list((self.records / "captures").glob("*.json"))), 2)

    def test_verifying_an_audit_verifies_its_fixed_findings_and_leaves_the_open_ones(self):
        before = self.shot()
        done = self.fixed(self.from_signal(before, "overlap"))
        amount = self.element_of(before, "#amount")["id"]
        waiting = self.from_signal(before, "tap-target", amount, pass_="accessibility", severity="P3")
        self.page.write_text(FIXED)
        outs = store.verify(P, before["audit"])
        self.assertEqual([o["finding"] for o in outs], [done["id"]])
        self.assertEqual(self.record(done["id"])["status"], "verified")
        self.assertEqual((self.record(waiting["id"])["status"], self.record(waiting["id"])["verification"]),
                         ("open", None))

    def test_verifying_an_audit_reports_a_finding_it_cannot_verify_and_goes_on_with_the_rest(self):
        before = self.shot()
        picture = self.tmp / "shot.png"
        picture.write_bytes(base.png(40, 30))
        image = store.capture(P, str(picture))[0]
        sent = self.fixed(store.add_finding(P, image["id"], region=[0, 0, 10, 10], pass_="color",
                                            rules=["wcag:1.4.3"], severity="P2", title="Low contrast",
                                            impact="Hard to read.", evidence="2:1", fix="Darker text."))
        done = self.fixed(self.from_signal(before, "overlap"))
        self.page.write_text(FIXED)
        outs = {o["finding"]: o for o in store.verify(P, before["audit"])}
        self.assertEqual(set(outs), {sent["id"], done["id"]})
        self.assertEqual((outs[sent["id"]]["result"], self.record(sent["id"])["status"]), ("error", "fixed"))
        self.assertIn("external picture", outs[sent["id"]]["message"])
        self.assertEqual((outs[done["id"]]["result"], self.record(done["id"])["status"]), ("pass", "verified"))


class AnchorTest(VerifyCase):
    def lead_finding(self):
        # the min-text-size finding on the 13 px lead paragraph, marked fixed
        self.page.write_text(LEAD)
        before = self.shot()
        lead = self.element_of(before, "header > p")
        return self.fixed(self.from_signal(before, "min-text-size", lead["id"], pass_="typography", severity="P3",
                                           title="The lead is 13 px on a phone"))

    def test_a_fixed_finding_whose_check_wrote_its_selector_from_the_root_is_found_by_its_selector_and_verified(self):
        f = self.lead_finding()
        self.assertEqual(f["anchor"]["selector"], "html > body > header > p")
        self.page.write_text(LEAD_FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        after = self.record(doc["verification"]["capture"])
        lead = self.element_of(after, "header > p")
        self.assertEqual((out["result"], doc["status"]), ("pass", "verified"), out["message"])
        self.assertEqual(doc["verification"]["anchor"],
                         {"element": lead["id"], "region": lead["box"], "found_by": "selector"})
        self.assertEqual(out["message"], f"{f['id']}  verified · min-text-size no longer fires on {after['id']}")

    def test_a_finding_found_again_by_its_old_position_only_reopens_saying_so(self):
        f = self.lead_finding()
        self.page.write_text(LEAD_GONE)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        after = doc["verification"]["capture"]
        self.assertEqual((out["result"], doc["status"], doc["verification"]["anchor"]["found_by"]),
                         ("fail", "reopened", "region"))
        self.assertEqual(out["message"], f"{f['id']} is still there: min-text-size fires again on {after} · anchor "
                                         "found by its old position only. Status: reopened.")
        self.assertEqual(doc["history"][-1]["why"],
                         f"still there on {after}: min-text-size · anchor found by its old position only")

    def test_a_check_that_no_longer_fires_at_the_old_position_only_waits_for_a_reviewer_and_never_verifies(self):
        # the element may have moved with its defect: only a person comparing the pictures can tell
        f = self.lead_finding()
        self.page.write_text(LEAD_EMPTY)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        after = doc["verification"]["capture"]
        self.assertEqual((out["result"], doc["status"], doc["verification"]["check"]), ("none", "fixed", "none"))
        self.assertEqual(doc["verification"]["anchor"]["found_by"], "region")
        self.assertEqual(out["message"], f"{f['id']}: min-text-size no longer fires on {after} · anchor found by its old "
                                         "position only, so its status stays fixed until a reviewer compares the before "
                                         f"and after and accepts it: dev visual verify {f['id']} --accept --why \"…\"")
        [out] = store.verify(P, f["id"], accept=True, why="the lead paragraph was removed from the header")
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["accepted_by"]),
                         ("accepted", "verified", "devuser"))


class BeforeElsewhereTest(VerifyCase):
    # The finding's capture was taken on another Mac (m-studio01): its record synced here, its picture and
    # description stayed there. verify captures the recipe again here and lets the check decide on the new capture;
    # with no picture before, nothing is compared, and the record and the outcome say so. The record names the Mac by
    # its machine id; the line a person reads names it as the dashboard does.
    def elsewhere(self, c):
        rec = self.record(c["id"])
        rec["by"]["host"], rec["image"]["where"] = "m-studio01", "local:m-studio01"
        store.save(P, rec)
        (self.local / "blobs" / f"{c['image']['sha256']}.png").unlink()
        (self.local / "desc" / f"{c['description']['sha256']}.json.gz").unlink()

    def test_a_check_finding_whose_picture_before_is_on_another_mac_is_verified_on_a_new_capture(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.elsewhere(before)
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        v = doc["verification"]
        self.assertEqual((out["result"], doc["status"], v["check"], v["error"]), ("pass", "verified", "pass", None), out["message"])
        self.assertEqual(self.record(v["capture"])["again_of"], before["id"])
        self.assertEqual((v["compared"], v["inside"], v["outside"], v["diff"]), (False, None, None, None))
        self.assertEqual(doc["history"][-1]["why"], f"overlap no longer fires on {v['capture']} · nothing compared: the "
                                                    "picture before was taken on m-studio01 and is not on this Mac")
        self.assertEqual(out["message"], f"{f['id']}  verified · overlap no longer fires on {v['capture']} · nothing "
                                         "compared: the picture before was taken on another Mac (m-studio01) and is "
                                         "not on this Mac")

    # Export moved and renamed, still over Amount: a page that changed (an unchanged one writes the same picture
    # back, which is then here to compare) where the check still fires
    def test_a_check_finding_whose_picture_before_is_elsewhere_reopens_when_the_check_still_fires(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.elsewhere(before)
        self.page.write_text(MOVED_EXPORT)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["compared"]), ("fail", "reopened", False))

    # a capture from before the machine id names its Mac by hostname: the line a person reads may show it, the
    # history, which syncs to the handoff repository, never takes it
    def test_the_history_never_takes_a_hostname_from_an_older_capture_before(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.elsewhere(before)
        rec = self.record(before["id"])
        rec["by"]["host"], rec["image"]["where"] = "bobs-macbook", "local:bobs-macbook"
        store.save(P, rec)
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual(doc["history"][-1]["why"], f"overlap no longer fires on {doc['verification']['capture']} · "
                                                    "nothing compared: the picture before was taken on another Mac and "
                                                    "is not on this Mac")
        self.assertNotIn("bobs-macbook", (self.records / "findings" / f"{f['id']}.json").read_text())
        self.assertIn("taken on bobs-macbook and is not on this Mac", out["message"])

    # judgment decides no fix by itself: with the picture before elsewhere, the reviewer looks at the new capture and
    # accepts it with a reason, the command the outcome names
    def test_a_judgment_finding_whose_picture_before_is_elsewhere_names_accept_and_accept_verifies_it(self):
        before = self.shot()
        f = self.fixed(store.add_finding(P, before["id"], region=[16, 80, 300, 60], pass_="hierarchy",
                                         rules=["nielsen:8"], severity="P2", title="The bar reads before the title",
                                         impact="The task is lost.", evidence="The bar pulls the eye first.",
                                         fix="Quieter bar."))
        self.elsewhere(before)
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        after = self.record(f["id"])["verification"]["capture"]
        self.assertEqual((out["result"], out["status"]), ("none", "fixed"))
        self.assertEqual(out["message"], f"{f['id']} came from judgment, and its picture before was taken on another "
                                         f"Mac (m-studio01) and is not on this Mac. Look at {after}, then accept it: "
                                         f"dev visual verify {f['id']} --accept --why \"…\"")
        [out] = store.verify(P, f["id"], accept=True, why="the title reads first on the new capture")
        self.assertEqual((out["result"], self.record(f["id"])["status"]), ("accepted", "verified"))


class BeforePrunedHereTest(VerifyCase):
    # This Mac took the finding's picture before and later pruned it from its store: the record names this Mac, so
    # "taken on <this Mac> and is not on this Mac" contradicted itself. The outcome says the picture is no longer in
    # this Mac's store and how to capture the screen again; the history, which syncs, says the Mac that took it no
    # longer has it
    def test_a_picture_before_this_mac_pruned_reads_as_gone_from_its_store_with_a_new_capture(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        self.assertEqual(self.record(before["id"])["image"]["where"], "local:m-test0001")
        (self.local / "blobs" / f"{before['image']['sha256']}.png").unlink()
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        after = doc["verification"]["capture"]
        self.assertEqual((out["result"], doc["status"], doc["verification"]["compared"]), ("pass", "verified", False))
        self.assertEqual(out["message"], f"{f['id']}  verified · overlap no longer fires on {after} · nothing compared: "
                                         "the picture before is no longer in this Mac's store; capture it again: "
                                         f"dev visual capture --again {before['id']}")
        self.assertEqual(doc["history"][-1]["why"], f"overlap no longer fires on {after} · nothing compared: the picture "
                                                    "before was taken on m-test0001 and is no longer in its store")


class JudgmentAndRefusalTest(VerifyCase):
    def judged(self, capture):
        return store.add_finding(P, capture["id"], region=[16, 80, 300, 60], pass_="hierarchy", rules=["nielsen:8"],
                                 severity="P2", title="The bar reads before the title", impact="The task is lost.",
                                 evidence="The bar pulls the eye first.", fix="Quieter bar.")

    def test_vd_ac_22_a_judgment_finding_waits_for_a_reviewer_and_accept_verifies_it(self):
        before = self.shot()
        f = self.fixed(self.judged(before))
        self.page.write_text(FIXED)
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["check"]), ("none", "fixed", "none"))
        self.assertIsNotNone(doc["verification"]["capture"])
        self.assertIn("--accept", out["message"])
        [out] = store.verify(P, f["id"], accept=True, why="the title reads first now")
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("accepted", "verified"))
        self.assertEqual(doc["verification"]["accepted_by"], "devuser")
        self.assertEqual((doc["history"][-1]["change"], doc["history"][-1]["why"]),
                         ("verified", "the title reads first now"))
        self.assertEqual(len(list((self.records / "captures").glob("*.json"))), 2, "accept takes no new capture")

    def test_accept_is_refused_without_a_reason_on_a_check_finding_and_before_a_verification(self):
        before = self.shot()
        judged = self.fixed(self.judged(before))
        with self.assertRaises(store.VisualError) as e:
            store.verify(P, judged["id"], accept=True)
        self.assertIn('--why "…"', str(e.exception))
        with self.assertRaises(store.VisualError) as e:
            store.verify(P, judged["id"], accept=True, why="looks fine")
        self.assertIn(f"dev visual verify {judged['id']}", str(e.exception))
        checked = self.fixed(self.from_signal(before, "overlap"))
        with self.assertRaises(store.VisualError) as e:
            store.verify(P, checked["id"], accept=True, why="looks fine")
        self.assertIn("check:overlap", str(e.exception))
        self.assertEqual((self.record(judged["id"])["status"], self.record(checked["id"])["status"]), ("fixed", "fixed"))

    def test_a_closed_finding_and_an_external_picture_are_not_verified(self):
        before = self.shot()
        f = self.from_signal(before, "overlap")
        store.close_finding(P, f["id"], "not-a-problem", "the overlap is a design choice")
        with self.assertRaises(store.VisualError) as e:
            store.verify(P, f["id"])
        self.assertIn("not-a-problem", str(e.exception))
        picture = self.tmp / "shot.png"
        picture.write_bytes(base.png(40, 30))
        image = store.capture(P, str(picture))[0]
        g = store.add_finding(P, image["id"], region=[0, 0, 10, 10], pass_="color", rules=["wcag:1.4.3"],
                              severity="P2", title="Low contrast", impact="Hard to read.", evidence="2:1",
                              fix="Darker text.")
        with self.assertRaises(store.VisualError) as e:
            store.verify(P, g["id"])
        self.assertIn("external picture", str(e.exception))
        self.assertIsNone(self.record(g["id"])["verification"])


class StructureFindingTest(VerifyCase):
    def test_a_structure_finding_is_verified_when_map_check_no_longer_finds_its_signal(self):
        vmap.add_node(P, "home", "Home", route="/", by="sati", why="the start")
        vmap.add_node(P, "settings", "Settings", route="/settings", by="sati", why="declared")
        audit = store.start(P, "product", "Can a clerk reach every screen?")
        c = store.capture(P, self.url, viewports=["390x844"], check=False)[0]
        rc, out, err = self.dev("map", "check")
        self.assertEqual(rc, 0, err)
        orphan = next(s for s in self.record(audit["id"])["signals"] if s["check"] == "orphan")
        f = store.add_finding(P, from_signal=f"{audit['id']}#{orphan['n']}", evidence_captures=[c["id"]], pass_="S2",
                              severity="P2", title="Settings is an orphan", impact="Nobody finds it.",
                              fix="Link it from home.")
        self.fixed(f)
        [out] = store.verify(P, f["id"])
        self.assertEqual((out["result"], self.record(f["id"])["status"]), ("fail", "reopened"))
        self.assertIn("dev visual map crawl", out["message"])
        vmap.add_edge(P, "home", "settings", trigger='link "Settings"', by="sati", why="the fix")
        store.set_finding(P, f["id"], status="fixed", why="linked from home")
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("pass", "verified"))
        self.assertEqual((doc["verification"]["check"], doc["verification"]["capture"]), ("pass", None))
        self.assertEqual(doc["verification"]["map_version"], vmap.load(P)["version"])


class CrawledStructureTest(VerifyCase):
    def page_at(self, name, links):
        body = "".join(f'<p><a href="{href}">{text}</a></p>' for href, text in links)
        (self.page.parent / name).write_text(f"<!doctype html><html lang=\"en\"><head><title>{name}</title></head>"
                                             f"<body><h1>{name.split('.')[0].title()}</h1>{body}</body></html>")

    def test_vd_ac_48_a_dead_end_is_verified_after_verify_crawls_the_app_again(self):
        base = self.url.rsplit("/", 1)[0]
        self.page_at("index.html", [("list.html", "List")])
        self.page_at("list.html", [("index.html", "Home"), ("detail.html", "Detail")])
        self.page_at("detail.html", [])
        audit = store.start(P, "product", "Can a clerk get back from a detail?", viewports=["390x844"])
        rc, out, err = self.dev("map", "crawl", f"{base}/index.html")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.count("✓ map v"), 1, out)
        rc, out, err = self.dev("map", "check")
        self.assertEqual(rc, 0, err)
        dead = next(s for s in self.record(audit["id"])["signals"] if s["check"] == "dead-end")
        c = store.capture(P, f"{base}/detail.html", check=False)[0]
        f = self.fixed(store.add_finding(P, from_signal=f"{audit['id']}#{dead['n']}", evidence_captures=[c["id"]],
                                         pass_="S2", severity="P2", title="The detail has no way on",
                                         impact="A clerk is stuck.", fix="Link back to the list."))
        crawled = vmap.load(P)["version"]
        self.page_at("detail.html", [("list.html", "Back to the list")])
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("pass", "verified"), out["message"])
        self.assertGreater(doc["verification"]["map_version"], crawled)
        self.assertIn("crawled again", doc["history"][-1]["why"])

    def test_a_journey_finding_is_verified_on_a_new_walk_with_a_capture_of_each_step(self):
        # the journey takes 3 steps (its screens) on a budget of 1; the fix raises the budget, and verify walks it
        # again
        base = self.url.rsplit("/", 1)[0]
        self.page_at("index.html", [("list.html", "List")])
        self.page_at("list.html", [("index.html", "Home"), ("detail.html", "Detail")])
        self.page_at("detail.html", [("list.html", "Back")])
        audit = store.start(P, "product", "Can a clerk open a detail?", viewports=["390x844"])
        rc, out, err = self.dev("map", "crawl", f"{base}/index.html")
        self.assertEqual(rc, 0, err)
        route = {n["route"]: n["id"] for n in vmap.load(P)["nodes"]}
        path = [route["/index.html"], route["/list.html"], route["/detail.html"]]
        vmap.add_journey(P, "J:open-detail", actor="clerk", task="open a detail", path=path,
                         do=["click text=List", "click text=Detail"], budget=1, by="sati", why="the task")
        rc, out, err = self.dev("journey", "walk", "J:open-detail", "--no-check")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.dev("map", "check")
        self.assertEqual(rc, 0, err)
        over = next(s for s in self.record(audit["id"])["signals"] if s["check"] == "journey-steps")
        walked = store.captures_of(P, audit["id"])
        f = self.fixed(store.add_finding(P, from_signal=f"{audit['id']}#{over['n']}", evidence_captures=[walked[0]["id"]],
                                         pass_="S1", severity="P2", title="Opening a detail takes too long",
                                         impact="A clerk loses time.", fix="Allow three steps."))
        vmap.add_journey(P, "J:open-detail", actor="clerk", task="open a detail", path=path,
                         do=["click text=List", "click text=Detail"], budget=3, by="sati", why="the fix")
        [out] = store.verify(P, f["id"])
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"]), ("pass", "verified"), out["message"])
        self.assertIn("walked J:open-detail again", doc["history"][-1]["why"])
        again = [c for c in store.captures_of(P, audit["id"]) if c["id"] not in {w["id"] for w in walked}]
        self.assertEqual(sorted(c["map"]["step"] for c in again), [1, 2, 3])


def png_with_square(width, height, square):
    # a white PNG with a black square [x, y, side] in picture pixels, written by hand like base.png
    x0, y0, side = square
    rows = []
    for y in range(height):
        row = bytearray(b"\xff\xff\xff" * width)
        if y0 <= y < y0 + side:
            row[x0 * 3:(x0 + side) * 3] = b"\x00\x00\x00" * side
        rows.append(b"\x00" + bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


@unittest.skipUnless(base.BROWSERS, base.NO_BROWSER or "")
class DocumentCompareTest(unittest.TestCase):
    def test_a_document_pages_box_is_compared_in_picture_pixels_whatever_its_point_scale(self):
        # a PDF page rendered at 2.5 picture px a point; its finding's box, like every box of a document capture, is
        # in picture pixels, so the change inside it counts inside and none outside
        tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-doc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        browsers = mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": base.BROWSERS})
        browsers.start()
        self.addCleanup(browsers.stop)
        records, local = tmp / "records", tmp / "store"
        (records / "captures").mkdir(parents=True)
        (local / "blobs").mkdir(parents=True)
        for cid, picture in (("VC-docbefore1", png_with_square(300, 200, (0, 0, 0))),
                             ("VC-docafter01", png_with_square(300, 200, (120, 60, 30)))):
            sha = hashlib.sha256(picture).hexdigest()
            (local / "blobs" / f"{sha}.png").write_bytes(picture)
            (records / "captures" / f"{cid}.json").write_text(json.dumps({
                "id": cid, "kind": "document", "recipe": {"page": 3, "converter": None, "scale": 2.5},
                "image": {"sha256": sha, "width": 300, "height": 200, "scale": 2.5}}))
        r = compare.compare_captures(records, local, "VC-docbefore1", "VC-docafter01", [110, 50, 50, 50])
        self.assertEqual(r["diff"]["scale"], 1)
        self.assertGreater(r["inside"], 0.3)
        self.assertEqual((r["outside"], r["regions_total"]), (0.0, 0))


class CommandTest(VerifyCase):
    def test_dev_visual_verify_prints_the_outcome_and_exits_1_while_the_finding_is_still_there(self):
        before = self.shot()
        f = self.fixed(self.from_signal(before, "overlap"))
        rc, out, err = self.dev("verify", f["id"])
        self.assertEqual(rc, 1, out)
        self.assertRegex(err, rf"✗ {f['id']} is still there: overlap .*\. Status: reopened\.")
        measured = re.escape(self.signal(before, "overlap")["measured"])
        self.assertRegex(out, rf"<<captured-data (\w+)>>\nmeasured {measured}\n<<end captured-data \1>>")
        store.set_finding(P, f["id"], status="fixed", why="moved Amount")
        self.page.write_text(FIXED_AND_HEADING)
        rc, out, err = self.dev("verify", f["id"])
        self.assertEqual(rc, 0, err)
        after = self.record(f["id"])["verification"]["capture"]
        self.assertRegex(out, rf"✓ {f['id']}  verified · overlap no longer fires on {after}")
        self.assertRegex(out, r"inside the box \d+\.\d% of the pixels changed, outside it \d+\.\d%")
        self.assertIn(f"new signal {after}#", out)
        self.assertIn(str(self.local / "blobs"), out)

    def test_dev_visual_compare_prints_the_numbers_and_changes_no_record(self):
        before = self.shot()
        self.page.write_text(base.PAGE.replace(HEADING, HEADING[:-1] + " position: relative; top: 20px; }"))
        after = store.capture_again(P, before["id"])[0]
        records = {p.name: p.read_text() for p in self.records.rglob("*.json")}
        rc, out, err = self.dev("compare", before["id"], after["id"])
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, rf"✓ {before['id']} → {after['id']} · [\d,]+ of [\d,]+ pixels changed \(\d+\.\d%\) · "
                              r"\d+ changed regions?")
        self.assertRegex(out, r"region 1 +\d+,\d+,\d+,\d+ +[\d,]+ pixels")
        diff = re.search(r"difference: (\S+\.png)", out).group(1)
        self.assertTrue(Path(diff).is_file())
        self.assertEqual({p.name: p.read_text() for p in self.records.rglob("*.json")}, records)

    def test_dev_visual_compare_refuses_two_captures_of_different_recipes(self):
        before = self.shot()
        other = store.capture(P, self.url, viewports=["1440x900"], check=False)[0]
        rc, out, err = self.dev("compare", before["id"], other["id"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("are captures of different recipes (viewport differ)", err)


@unittest.skipUnless(base.NODE and base.GITLEAKS, "node and gitleaks redact the text a finding holds")
class ExternalPictureTest(base.StoreCase):
    # a picture someone sent has no recipe to capture again: its findings are verified by eye
    def fixed_on_picture(self):
        store.start(P, "screen", "Is the screenshot readable?")
        picture = self.tmp / "shot.png"
        picture.write_bytes(base.png(40, 30))
        image = store.capture(P, str(picture))[0]
        f = store.add_finding(P, image["id"], region=[0, 0, 10, 10], pass_="color", rules=["wcag:1.4.3"],
                              severity="P2", title="Low contrast", impact="Hard to read.", evidence="2:1",
                              fix="Darker text.")
        return store.set_finding(P, f["id"], status="fixed", why="darker text", commit="4f2a9c1")

    def test_a_reviewer_accepts_the_fix_of_a_finding_on_an_external_picture(self):
        f = self.fixed_on_picture()
        [out] = store.verify(P, f["id"], accept=True, why="checked by eye on a new capture of the screen")
        doc = self.record(f["id"])
        self.assertEqual((out["result"], doc["status"], doc["verification"]["accepted_by"]),
                         ("accepted", "verified", "devuser"))
        self.assertEqual((doc["history"][-1]["change"], doc["history"][-1]["why"]),
                         ("verified", "checked by eye on a new capture of the screen"))

    def test_a_fixed_finding_can_be_closed_without_a_verification(self):
        f = self.fixed_on_picture()
        store.close_finding(P, f["id"], "not-a-problem", "the screenshot shows a retired screen")
        doc = self.record(f["id"])
        self.assertEqual((doc["status"], doc["history"][-1]["why"]),
                         ("not-a-problem", "the screenshot shows a retired screen"))


class AnchorMatchTest(unittest.TestCase):
    # store._found_again on description elements written by hand, so each way of finding the anchor again is
    # checked alone: the anchor's keys come from the checks' writer, the elements' from the description's
    OLD = [16, 48, 358, 40]

    def el(self, eid, role="paragraph", box=(0, 0, 10, 10), **kw):
        return {"id": eid, "role": role, "name": kw.get("name"), "text": kw.get("text"),
                "locator": kw.get("locator"), "selector": kw.get("selector"), "box": list(box)}

    def found(self, elements, anchor, before=None):
        at = store._found_again(elements, {"region": self.OLD, **anchor}, *([before] if before else []))
        return at["element"], at["found_by"]

    def test_a_selector_written_from_the_root_or_spaced_otherwise_matches_the_descriptions_short_form(self):
        elements = [self.el("E1", selector="header > ul > li"), self.el("E2", selector="header > p"),
                    self.el("E3", selector="main > p:nth-of-type(2)"), self.el("E4", selector="#a\\>b > p"),
                    self.el("E5", selector="#a\\ > b > p")]
        for written, element in (("html > body > header > p", "E2"), ("html > main>p:nth-of-type(2)", "E3"),
                                 ("html>body>header  >  ul > li", "E1"),
                                 # an escaped > is part of the id, not a combinator
                                 ("html > #a\\>b>p", "E4")):
            with self.subTest(written):
                self.assertEqual(self.found(elements, {"selector": written}), (element, "selector"))

    def test_the_locator_comes_first_then_the_selector_then_the_role_and_name_then_the_old_position(self):
        anchor = {"locator": "getByRole('button', { name: 'Export' })", "selector": "html > body > main > button",
                  "name": 'button "Export"'}
        by_locator = self.el("E1", "button", locator=anchor["locator"], selector="nav > button", name="Send")
        by_selector = self.el("E2", "button", selector="main > button", name="Save")
        by_name = self.el("E3", "button", selector="aside > button", name="Export")
        there = self.el("E4", box=self.OLD, selector="footer > p", text="Totals")
        elements = [there, by_name, by_selector, by_locator]
        self.assertEqual(self.found(elements, anchor), ("E1", "locator"))
        self.assertEqual(self.found(elements[:3], anchor), ("E2", "selector"))
        self.assertEqual(self.found(elements[:2], anchor), ("E3", "role and name"))
        # when two elements share the role and name, neither is the anchor
        twin = self.el("E5", "button", selector="dialog > button", name="Export")
        self.assertEqual(self.found([there, by_name, twin], anchor), ("E4", "region"))

    def test_an_anchor_with_its_own_test_id_or_id_is_found_by_it_in_either_spelling(self):
        # the checks write the element's own test id or id as its selector; the description can hold it as a locator
        elements = [self.el("E1", "button", locator="getByTestId('export')", selector="div > button:nth-of-type(1)"),
                    self.el("E2", "button", locator="locator('#amount')"),
                    self.el("E3", "region", selector="#journeys", name="Journeys")]
        self.assertEqual(self.found(elements, {"selector": '[data-testid="export"]'}), ("E1", "test id"))
        self.assertEqual(self.found(elements, {"selector": "#amount"}), ("E2", "id"))
        # an id at the start of a path is an ancestor's, not the element's own
        self.assertEqual(self.found(elements, {"selector": "#journeys > article > p"}), (None, "region"))

    def test_the_before_descriptions_own_keys_are_tried_when_the_checks_wrote_the_selector_from_an_id(self):
        # the checks write the path from the nearest id; the description writes the shortest unique one, on both
        # captures alike
        anchor = {"element": "E9", "selector": "#journeys > article:nth-of-type(1) > ol > li:nth-of-type(2) > p",
                  "name": 'paragraph "Invoices"'}
        before = {"elements": [self.el("E9", selector="ol > li:nth-of-type(2) > p", text="Invoices")]}
        elements = [self.el("E12", selector="ol > li:nth-of-type(2) > p", text="Invoices"),
                    self.el("E14", selector="ol > li:nth-of-type(3) > p", text="Invoices")]
        self.assertEqual(self.found(elements, anchor, before), ("E12", "selector"))
        self.assertEqual(self.found(elements, anchor), (None, "region"))


if __name__ == "__main__":
    unittest.main()
