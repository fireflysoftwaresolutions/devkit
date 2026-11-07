# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The instruction canary's role check (the developer, M346; lesson L-fy2lfvacp64): a final message names crew members
# as `<role> (<Name>)`, the role in any language, never the Matrix name alone. The check reads the structure (the
# roster's name inside parentheses right after other words), never a list of role words. The status block's header
# and Crew line are prose too; code spans, code blocks and links are not. The main member is checked like every
# other (the developer's decision): "Tech lead (Morpheus)", and reports end with "Needs from the tech lead:".
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_canary
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))

from devkit import canary, crew  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, never the team's changes in the developer's handoff repository


def bare(text):
    return canary.bare_names(text, ROSTER)


class RoleFormTests(unittest.TestCase):
    def test_a_name_standing_alone_in_prose_is_found(self):
        self.assertEqual(bare("The Keymaker has fixed the guard, and Tank reviewed it."), ["The Keymaker", "Tank"])

    def test_the_role_form_passes_in_any_language(self):
        for text in ("Security engineer (The Keymaker) has fixed the guard.",
                     "El ingeniero de backend (Tank) lo revisó.",
                     "バックエンドエンジニア（Tank）が確認しました。",
                     "the Backend engineer (Tank)'s 47ea30b changed the curve"):
            self.assertEqual(bare(text), [], text)

    def test_parentheses_without_a_role_before_them_do_not_pass(self):
        self.assertEqual(bare("(Tank) reviewed it; ask (Sati, Trinity) next."), ["Tank", "Sati", "Trinity"])

    def test_the_article_and_its_lowercase_form_count_as_the_name(self):
        self.assertEqual(bare("I asked the Keymaker, then Oracle answered."), ["The Keymaker", "The Oracle"])
        self.assertEqual(bare("Business analyst (the Oracle) answered."), [])

    def test_the_status_block_header_and_crew_line_are_prose(self):
        member = "**[Firefly> DevKit · Kid]** devkit · `main` · tooling\n- **Done:** x\n- **Next:** y"
        self.assertEqual(bare(member), ["Kid"])
        self.assertEqual(bare("**[Firefly> DevKit · DevEx engineer (Kid)]** devkit · `main` · tooling"), [])
        crew_line = "- **Crew:** Link ✓ release branch · Code reviewer (Seraph) ▸ review of the merge"
        self.assertEqual(bare(crew_line), ["Link"])

    def test_code_spans_code_blocks_links_and_paths_are_not_prose(self):
        text = ("Run `dev crew brief link \"…\"` and `Tank`.\n```bash\ndev crew ask --to Tank \"…\"\n```\n"
                "See https://example.com/Tank and /tmp/kid-approvals/Tank.md or tests/test_link.py.")
        self.assertEqual(bare(text), [])

    def test_an_alias_counts_and_so_does_the_main_member(self):
        self.assertEqual(bare("the scout found it; Needs from Morpheus: nothing"), ["Sparks", "Morpheus"])
        self.assertEqual(bare("the scout found it; Needs from the tech lead: nothing"), ["Sparks"])

    def test_a_word_that_only_contains_a_name_is_not_one(self):
        self.assertEqual(bare("Tanks, Linked, Kidney, Neon and Sparkle are not crew names."), [])


class MemberHeaderTests(unittest.TestCase):
    # a member's header carries its role now, in the message's language: a long one is still the status block
    def test_a_header_with_a_long_role_in_another_language_is_the_status_block(self):
        text = ("**[Firefly> DevKit · Ingeniera de experiencia de desarrollo y herramientas (Kid)]** devkit · `main` · "
                "x\n- **Done:** x\n- **Check:** ✓ 3 tests\n- **Next:** y")
        self.assertTrue(canary.has_marker(text))
        self.assertEqual(canary.CREW_RE.match(text).group(1),
                         "Ingeniera de experiencia de desarrollo y herramientas (Kid)")
        self.assertEqual(bare(text), [])


class ReasonTests(unittest.TestCase):
    def test_the_reason_gives_each_name_its_role_form(self):
        why = canary.roles_reason(["Tank", "The Keymaker"], ROSTER)
        self.assertIn("Backend engineer (Tank)", why)
        self.assertIn("Security engineer (The Keymaker)", why)
        self.assertIn("code spans", why)


if __name__ == "__main__":
    unittest.main()
