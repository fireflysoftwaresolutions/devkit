# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Skill enforcement rules. Run: python3 -m unittest discover -s tests -v"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import skillguard as sg  # noqa: E402

HAVE = {"frontend-design", "impeccable", "docs", "backend", "java-springboot", "delivery", "devsecops", "diagrams",
        "github-actions-hardening", "react-best-practices", "communicating"}


def tool_use(name, **inp):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


class SkillRuleTests(unittest.TestCase):
    def setUp(self):
        self._installed = sg.installed
        sg.installed = lambda agent: HAVE

    def tearDown(self):
        sg.installed = self._installed

    def test_each_kind_of_work_needs_its_skills(self):
        need = {aid: {s for r in sg.rules_for_activity(aid) for s in sg.required(r, agent)}
                for aid, agent in (("testing", "codex"), ("cicd", "claude"), ("ux", "claude"), ("docs", "claude"))}
        self.assertIn("testing", need["testing"])
        self.assertIn("cicd", need["cicd"])
        self.assertLessEqual({"frontend-design", "impeccable"}, need["ux"])
        self.assertIn("docs", need["docs"])

    def test_brief_gets_required_skills(self):
        # the member's kind of work decides, not the words (no English UI word here)
        upd = sg.on_agent_brief("claude", {"prompt": "Ajusta el encabezado", "description": "Trinity: encabezado"})
        self.assertIn("## devkit · required skills", upd["prompt"])
        self.assertIn("`frontend-design`", upd["prompt"])
        self.assertIsNone(sg.on_agent_brief("claude", upd))  # not appended twice
        named = sg.on_agent_brief("claude", {"prompt": "Use frontend-design and impeccable to rework the layout",
                                             "description": "Trinity: rework the layout"})
        self.assertNotIn("required skills", named["prompt"])  # the skills are named already
        # every crew member reports the same way, its role first
        self.assertIn("**[Firefly> DevKit · Frontend engineer (Trinity)]**", named["prompt"])
        self.assertIsNone(sg.on_agent_brief("claude", {"prompt": "Use frontend-design and impeccable to rework the "
                                                                 "layout. End with **[Firefly> DevKit · Trinity]**"}))
        self.assertIsNone(sg.on_agent_brief("codex", {"prompt": "Rework the dashboard layout"}))

    def test_rules_for_files_by_path_and_content(self):
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "page.py"
            page.write_text('HTML = "<!doctype html><style>body{}</style>"')
            plain = Path(d) / "tool.py"
            plain.write_text("print(1)")
            svc = Path(d) / "InvoiceService.java"
            svc.write_text("import org.springframework.stereotype.Service;")
            pojo = Path(d) / "Money.java"
            pojo.write_text("record Money(long cents) {}")
            ids = {r["id"]: [Path(f).name for f in fs] for r, fs in sg.rules_for_files(
                [str(page), str(plain), str(svc), str(pojo), "/r/.github/workflows/ci.yml", "/r/src/app.tsx"])}
            self.assertEqual(ids["ui"], ["app.tsx", "page.py"])
            self.assertEqual(ids["spring"], ["InvoiceService.java"])
            self.assertIn("workflows", ids)
            self.assertIn("react", ids)

    def test_stop_requires_skills_once(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.jsonl"
            t.write_text("\n".join([tool_use("Skill", skill="devkit:docs"),
                                    tool_use("Edit", file_path="/r/src/styles.css", old_string="a", new_string="b"),
                                    tool_use("Write", file_path="/r/README.md", content="x")]) + "\n")
            sess = {}
            why = sg.on_stop("claude", {"transcript_path": str(t)}, sess)
            self.assertIn("`frontend-design`", why)
            self.assertNotIn("`docs`", why)  # loaded through the Skill tool
            self.assertIn("styles.css", why)
            self.assertIn("docs", sess["skills_loaded"])
            with t.open("a") as fh:
                fh.write(tool_use("Edit", file_path="/r/src/other.css", old_string="a", new_string="b") + "\n")
            self.assertIsNone(sg.on_stop("claude", {"transcript_path": str(t)}, sess))  # once per skill
            with t.open("a") as fh:
                fh.write(tool_use("Read", file_path="/x/skills/frontend-design/SKILL.md") + "\n")
            sg.on_stop("claude", {"transcript_path": str(t)}, sess)
            self.assertIn("frontend-design", sess["skills_loaded"])

    def test_codex_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "rollout.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "payload": {"instructions": "- impeccable (file: /p/skills/impeccable/SKILL.md)"}}),
                json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                            "arguments": json.dumps({"cmd": "sed -n 1,80p /c/skills/docs/SKILL.md"})}}),
                json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                            "input": "*** Begin Patch\n*** Update File: web/app.css\n@@\n*** End Patch"}}),
            ]
            t.write_text("\n".join(lines) + "\n")
            loaded, files, _ = sg.scan("codex", str(t))
            self.assertEqual(loaded, {"docs"})  # the skill list in the instructions does not count
            self.assertEqual(files, ["web/app.css"])
            why = sg.on_stop("codex", {"transcript_path": str(t)}, {})
            self.assertIn("`impeccable`", why)

    def test_user_rules_extend_devkit_rules(self):
        saved = sg.CFG.get("skills")
        sg.CFG["skills"] = {"rules": [{"id": "ui", "why": "house style", "skills": {"claude": ["brand"]}}]}
        try:
            r = next(r for r in sg.rules() if r["id"] == "ui")
            self.assertEqual((r["why"], r["skills"]["claude"]), ("house style", ["brand"]))
            self.assertIn("*.css", r["files"])  # kept from devkit's rule
        finally:
            sg.CFG["skills"] = saved


if __name__ == "__main__":
    unittest.main()
