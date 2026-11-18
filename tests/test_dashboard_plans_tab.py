# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A page's section tabs (Overview, Tasks, ..., Plans, Sessions) scroll sideways in their bar when they do not fit, as
# on a 390 px phone. The selected tab stays in the bar's view: opened from a link, chosen with a click, or reached with
# the arrow keys, it is never left past the bar's edge. In headless Chromium, from the dashboard tests' fixture
# (tests/test_dashboard.py), whose setup and probe these tests reuse; below 500 px the page runs in an iframe that wide.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_dashboard_plans_tab
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard as T  # noqa: E402  (the headless Chrome harness and the fixture)

# where the selected tab is against its bar: its left and right edges, the bar's, and how far the bar can scroll
WHERE = """const seen = () => { const bar = $(".ptabs"), tab = $('.ptab[aria-selected="true"]'), b = bar.getBoundingClientRect(),
    t = tab.getBoundingClientRect(); return {tab: text(tab), id: tab.id, inside: t.left >= b.left - 0.5 && t.right <= b.right + 0.5,
    scrolls: bar.scrollWidth > bar.clientWidth, page: document.documentElement.scrollWidth}; };"""


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class SelectedTabInView(unittest.TestCase):
    setUpClass = T.PageBehaviorTests.__dict__["setUpClass"]
    tearDownClass = T.PageBehaviorTests.__dict__["tearDownClass"]
    probe = T.PageBehaviorTests.probe

    def test_a_projects_plans_tab_opened_from_a_link_at_390_px_is_in_its_bars_view(self):
        out = self.probe(WHERE + "out.at = seen();", 390, "#/projects/acme/widget?tab=plans")
        self.assertTrue(out["at"]["scrolls"], "the tabs fit at 390 px, so the bar never scrolls and this proves nothing")
        self.assertEqual((out["at"]["tab"], out["at"]["inside"]), ("Plans 2", True), out["at"])
        self.assertLessEqual(out["at"]["page"], 390, "the page scrolls sideways")

    def test_a_tab_reached_with_the_arrow_keys_or_a_click_at_390_px_stays_in_its_bars_view(self):
        # the arrow keys move from the focused tab, as for a keyboard user who tabbed into the bar
        steps = WHERE + """$('.ptab[aria-selected="true"]').focus();
            out.keys = []; for (let i = 0; i < 7; i += 1) { key($('.ptab[aria-selected="true"]'), "ArrowLeft");
            await tick(150); out.keys.push(seen()); }
            $("#tab-note").click(); await tick(150); out.click = seen();"""
        out = self.probe(steps, 390, "#/projects/acme/widget?tab=plans")
        self.assertEqual([k["id"] for k in out["keys"]], ["tab-worktrees", "tab-branches", "tab-prs", "tab-tasks",
                                                         "tab-overview", "tab-note", "tab-sessions"])
        self.assertEqual([k["id"] for k in out["keys"] if not k["inside"]], [], out["keys"])
        self.assertEqual((out["click"]["id"], out["click"]["inside"]), ("tab-note", True), out["click"])


if __name__ == "__main__":
    unittest.main()
