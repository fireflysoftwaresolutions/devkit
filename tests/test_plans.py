# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Plan records keep their whole body: only the record's own changelog, at the end, is read as the changelog."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, plans, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
ENTRY = "v1 · 2026-11-07T10:00:00Z · ana@mbp1 (claude:s1) · created"


def record(body: str, entries=(ENTRY,)) -> str:
    """A plan file as `dev plan` writes it: frontmatter, the body, then the record's changelog."""
    return ("---\nid: P-test\ntitle: Plan\nversion: 1\n---\n" + body + "\n\n## Changelog\n"
            + "".join(f"- {e}\n" for e in entries))


class TempPlans:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / ".handoff"
        root.mkdir()
        self.saved = (core.HANDOFF, work.HANDOFF, work.ITEMS, plans.PLANS, plans.HANDOFF)
        core.HANDOFF = work.HANDOFF = plans.HANDOFF = root
        work.ITEMS, plans.PLANS = root / "items", root / "plans"
        return self

    def __exit__(self, *exc):
        (core.HANDOFF, work.HANDOFF, work.ITEMS, plans.PLANS, plans.HANDOFF) = self.saved
        self.tmp.cleanup()

    def raw(self, text: str) -> Path:
        p = plans.PLANS / "acme" / "api" / "spec.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class RecordChangelogTests(unittest.TestCase):
    def test_a_changelog_heading_that_is_not_the_records_own_stays_in_the_body(self):
        bodies = {
            "fenced, in the middle": "# Plan\n\nIntro.\n\n```markdown\n# PRODUCT.md\n\n## Changelog\n"
                                     "- 26.11.0 first release\n```\n\n## Rollout\n\nShip it.",
            "fenced, at the end": "# Plan\n\nThe file:\n\n```markdown\n## Changelog\n- v2 · second\n- v1 · first\n```",
            # a backtick fence, a tilde fence, and a four-backtick fence whose ``` lines are its content
            "fenced, several times": "# Plan\n\n```text\n## Changelog\n- a\n```\n\nBetween.\n\n~~~\n## Changelog\n"
                                     "- b\n~~~\n\n````markdown\n```text\n## Changelog\n```\n- c\n````\n\nAfter.",
            "a section of the text": "# Plan\n\n## Changelog\n\nThe product keeps a CHANGELOG.md.\n\n## Rollout\n\n- one",
            "fenced, before a fence left open": "Intro.\n\n```text\n## Changelog\n- example\n```\n\nOpen:\n\n```py\nx = 1",
        }
        for shape, body in bodies.items():
            with self.subTest(shape):
                meta, got, log = plans._split(record(body, entries=("v2 · second", "v1 · first")))
                self.assertEqual(got, body)
                self.assertEqual(log, ["v2 · second", "v1 · first"])
                self.assertEqual(meta["id"], "P-test")

    def test_a_changelog_heading_that_does_not_end_the_record_is_body(self):
        raws = {
            "followed by a paragraph": "# Plan\n\n## Changelog\n\nWe keep one per release.\n\n- step one\n- step two\n",
            "followed by another section": "# Plan\n\n## Changelog\n\n- 26.11.0 first\n\n## Rollout\n\n- step one\n",
        }
        for shape, raw in raws.items():
            with self.subTest(shape):
                self.assertEqual(plans._split(raw), ({}, raw.strip("\n"), []))

    def test_an_empty_frontmatter_keeps_the_rules_and_key_lines_of_the_body(self):
        body = "# Plan\n\nIntro.\n\n---\n\nstatus: done\n\n---\n\nEnd."
        meta, got, log = plans._split("---\n---\n" + body + "\n")
        self.assertEqual((meta, got, log), ({}, body, []))


class ReviseTests(unittest.TestCase):
    def test_revise_round_trips_a_large_body_unchanged(self):
        lines = ["# Big spec", ""] + [f"Requirement {i}: the parser keeps line {i}." for i in range(1, 299)]
        lines += ["", "```markdown", "# PRODUCT.md", "", "## Changelog", "- 26.11.0 first release", "```", ""]
        lines += ["---", "status: draft", "---", ""] + [f"- detail {i}, with - dashes" for i in range(1, 300)]
        lines += ["", "~~~", "## Changelog", "~~~", ""] + [f"Section text {i}." for i in range(1, 380)]
        lines += ["", "```text", "## Changelog", "- the last example", "```"]
        raw = "\n".join(lines) + "\n"
        self.assertEqual(len(lines), 1000)
        with TempPlans() as t:
            path = t.raw(raw)
            plans.revise(path, "adopt the hand-written spec", a=A)
            plans.revise(path, "second revision", a=A)
            text = path.read_text()
            meta, body, log = plans._split(text)
            self.assertEqual(len(body.splitlines()), 1000, "lines of the body kept after two revisions")
            self.assertEqual(body, raw.strip("\n"))
            self.assertIn(raw.strip("\n"), text)
            self.assertEqual([e.split(" · ")[0] for e in log], ["v2", "v1"])
            self.assertEqual(meta["version"], "2")
            self.assertFalse(plans.needs_revision(path))

    def test_revise_refuses_to_write_a_record_it_would_shrink(self):
        body = "# Plan\n\nKeep this line.\n\nAnd this one."
        with TempPlans() as t:
            path = t.raw(record(body))
            before = path.read_text()
            real = plans._read

            def misreading(text):  # a parser bug: the last line of the body goes missing
                meta, extra, b, log = real(text)
                return meta, extra, b.rsplit("\n", 1)[0], log

            with mock.patch.object(plans, "_read", misreading):
                with self.assertRaises(work.RecordLossError) as cm:
                    plans.revise(path, "a revision", a=A)
            self.assertIn("And this one.", str(cm.exception))
            self.assertIn("1 line", str(cm.exception))
            self.assertEqual(path.read_text(), before)

    def test_adopting_keeps_every_line_of_a_plan_that_starts_with_three_dashes(self):
        raws = {
            "another tool's frontmatter": "---\nauthor: ana\ntags: [api, export]\n---\n# Plan\n\nBody.\n",
            "a first --- rule": "---\n\n# Plan\n\nIntro.\n\n---\n\nMore.\n",
        }
        for shape, raw in raws.items():
            with self.subTest(shape), TempPlans() as t:
                path = t.raw(raw)
                meta = plans.revise(path, "adopt the hand-written plan", a=A)
                self.assertEqual(plans._split(path.read_text())[1], raw.strip("\n"))
                self.assertEqual((meta["title"], meta["id"][:2]), ("Plan", "P-"))
                self.assertFalse(plans.needs_revision(path))

    def test_revise_keeps_the_frontmatter_lines_devkit_does_not_know(self):
        extra = ["owner: ana", "reviewers:", "  - bob"]
        with TempPlans() as t:
            path = t.raw(record("# Plan\n\nBody.").replace("version: 1\n", "version: 1\n" + "\n".join(extra) + "\n"))
            plans.revise(path, "a revision", a=A)
            front, _ = work.split_frontmatter(path.read_text())
            self.assertEqual([ln for ln in front.splitlines() if ln in extra], extra)
            self.assertIn("version: 2", front.splitlines())


if __name__ == "__main__":
    unittest.main()
