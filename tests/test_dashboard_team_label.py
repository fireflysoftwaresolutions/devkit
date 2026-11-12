# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Since the machine id came, the dashboard keys a person by user@<machine id> (bob@m-0000beef), which says nothing to
# a reader. The server resolves the hostname for each person (people[].label, from work.display): the Team page, the
# person drawer, the "Who is on it" rows, the avatars and the runs of another Mac show that hostname, with the machine
# id beside it, and a person named by a hostname already shows as before. In headless Chromium, from the dashboard
# tests' fixture (tests/test_dashboard.py), whose setup and probe these tests reuse.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_dashboard_team_label
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard as T  # noqa: E402  (the headless Chrome harness and the fixture)
from devkit import work  # noqa: E402

BOB, BOB_ID = "bob@m-0000beef", "m-0000beef"


def bob_on_mini(d):
    # bob's Mac as people() names it now: the machine id in `who` and `host`, and the hostname the server resolved
    # for it in `label`; a person named by a hostname has that hostname as its label. bob runs a plan there.
    for x in d["people"]:
        x.setdefault("label", x["host"])
        if x["who"] == "bob@studio":
            x.update(who=BOB, host=BOB_ID, label="mini")
            x["sessions"][0]["progress"] = work.run_summary(
                {"goal": "Rotate the keys", "state": "running", "agent": "codex",
                 "plan": [{"name": "rotate", "state": "running", "est": 30, "min": 5}, {"name": "verify", "est": 10}]})


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class TeamLabelTests(unittest.TestCase):
    setUpClass = T.PageBehaviorTests.__dict__["setUpClass"]
    tearDownClass = T.PageBehaviorTests.__dict__["tearDownClass"]
    probe = T.PageBehaviorTests.probe
    variant = T.PageBehaviorTests.variant

    def test_a_person_shows_the_hostname_with_the_machine_id_beside_it(self):
        out = self.probe("""const card = who => $$(".pcard3").find(c => $("h3 .rl", c) && text($("h3 .rl", c)).startsWith(who));
            const bob = card("bob@"), me = card("me@testmac");
            out.name = text($("h3 .rl", bob)); out.id = text($("h3 .mid", bob)); out.av = $(".av", bob).getAttribute("aria-label");
            out.me = text($("h3 .rl", me)); out.meId = $("h3 .mid", me);
            await go("#/team?modal=" + encodeURIComponent("person:bob@m-0000beef"));
            out.title = text($("#modal-title")); out.pline = text($("#modal .pline"));""",
                         hash_="#/team", page=self.variant(bob_on_mini))
        self.assertEqual((out["name"], out["id"]), ("bob@mini", BOB_ID))
        self.assertTrue(out["av"].startswith("bob@mini (m-0000beef)"), out["av"])
        self.assertEqual((out["me"], out["meId"]), ("me@testmac", None))  # a hostname already: nothing beside it
        self.assertEqual(out["title"], "bob@mini")
        self.assertTrue(out["pline"].startswith(BOB_ID), out["pline"])

    # on a phone the name keeps its room: the machine id gives way first, and nothing scrolls sideways
    def test_on_a_phone_the_name_keeps_its_room_and_the_page_never_scrolls_sideways(self):
        out = self.probe("""const bob = $$(".pcard3").find(c => $("h3 .rl", c) && text($("h3 .rl", c)).startsWith("bob@"));
            const name = $("h3 .rl", bob), id = $("h3 .mid", bob);
            out.cut = name.scrollWidth > name.clientWidth + 1; out.id = id.getBoundingClientRect().width;
            out.sideways = document.documentElement.scrollWidth > innerWidth + 1;""",
                         width=390, hash_="#/team", page=self.variant(bob_on_mini))
        self.assertEqual((out["cut"], out["sideways"]), (False, False))
        self.assertGreater(out["id"], 0)

    def test_a_run_on_another_mac_names_that_mac_by_its_hostname(self):
        out = self.probe("""out.card = text($$("#runs .rcard").find(c => text(c).includes("Rotate the keys")));""",
                         hash_="#/background", page=self.variant(bob_on_mini))
        self.assertIn("on mini", out["card"])
        self.assertNotIn(BOB_ID, out["card"])


if __name__ == "__main__":
    unittest.main()
