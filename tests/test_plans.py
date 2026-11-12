# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Plan records keep their whole body: only the record's own changelog, at the end, is read as the changelog."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, core, plans, work  # noqa: E402

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

    # An approval covers the version that was approved. A plan changed after it used to stay "approved", so the
    # dashboard and `dev plan ls` showed an approval nobody gave to the new text.
    def test_an_approved_plan_revised_without_a_status_goes_back_to_draft_and_says_which_version_was_approved(self):
        with TempPlans() as t:
            path = t.raw(record("# Plan\n\nShip CSV."))
            self.assertEqual(plans.revise(path, "the team agreed", a=A, status="approved")["status"], "approved")
            self.assertEqual(plans.revise(path, "a reason, the text unchanged", a=A)["status"], "approved")
            path.write_text(path.read_text().replace("Ship CSV.", "Ship CSV and XLSX."))
            meta = plans.revise(path, "customer asked for XLSX too", a=A)
            self.assertEqual((meta["status"], meta["version"]), ("draft", 4))
            again, _, log = plans._split(path.read_text())
            self.assertEqual(again["status"], "draft")
            self.assertIn("revised; back to draft: the approval covered v3 — why: customer asked for XLSX too", log[0])
            self.assertEqual(plans.revise(path, "approved again", a=A, status="approved")["status"], "approved")

    def test_revise_keeps_the_frontmatter_lines_devkit_does_not_know(self):
        extra = ["owner: ana", "reviewers:", "  - bob"]
        with TempPlans() as t:
            path = t.raw(record("# Plan\n\nBody.").replace("version: 1\n", "version: 1\n" + "\n".join(extra) + "\n"))
            plans.revise(path, "a revision", a=A)
            front, _ = work.split_frontmatter(path.read_text())
            self.assertEqual([ln for ln in front.splitlines() if ln in extra], extra)
            self.assertIn("version: 2", front.splitlines())


class PlanCommandTests(unittest.TestCase):
    def test_a_plan_path_that_does_not_exist_is_a_plain_error_that_names_it(self):
        # a typo in the path is the caller's mistake: exit 2 and the path, never devkit's bug report (exit 70)
        with TempPlans():
            missing = plans.PLANS / "acme" / "api" / "no-such-plan.md"
            for verb in ("revise", "approve", "supersede"):
                with self.subTest(verb=verb):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = cli.main(["plan", verb, str(missing), "--why", "a reason"])
                    self.assertEqual(rc, 2, err.getvalue())
                    self.assertIn(f"no plan at {missing}:", err.getvalue())  # as typed
                    self.assertNotIn("devkit bug", err.getvalue() + out.getvalue())
                    self.assertFalse(missing.exists())


class NewPlanMarkdownTests(unittest.TestCase):
    # `dev docs check` runs markdownlint on plans, and a fresh plan of any kind failed it three times. markdownlint
    # reads the front-matter title as the document's top-level heading, so a `# title` in the body is a second one
    # (MD025); the record's changelog heading and its list need a blank line between them (MD022, MD032).
    def test_a_new_plan_of_each_kind_has_one_title_and_a_set_off_changelog(self):
        for kind in plans.KINDS:
            with self.subTest(kind=kind), TempPlans():
                text = plans.new("acme/api", "Invoice export", a=A, kind=kind).read_text()
                front, body = work.split_frontmatter(text)
                self.assertIn("title: Invoice export", front.splitlines())
                self.assertEqual([ln for ln in body.splitlines() if ln.startswith("# ")], [])
                self.assertRegex(body, r"\S\n\n## Changelog\n\n- v1 · .* · created from the " + kind + r" template\n$")


if __name__ == "__main__":
    unittest.main()
