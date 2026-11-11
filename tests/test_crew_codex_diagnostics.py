# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (fixtures exercise the unfinished-work gate)
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import gates  # noqa: E402
from tests.test_crew_gates import KID_DOCS, GateCase, git  # noqa: E402


class CodexDiagnosticProofs(GateCase):
    def setUp(self):
        super().setUp()
        self.scratch = Path(self.tmp.name) / "scratchpad" / "kid"
        self.scratch.mkdir(parents=True)
        self.probe = self.scratch / "probe.py"
        self.probe.write_text("print('diagnostic evidence')\n")

    def codex(self, paths, commands=(), skills=True):
        records = [{"timestamp": "2026-11-08T09:00:00.000Z", "type": "session_meta",
                    "payload": {"id": "diagnostic"}}]
        for path in paths:
            records.append({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "apply_patch",
                "input": f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch"}})
        shell = list(commands) + ["dev test", 'dev progress step "diagnosed" --check "controls pass"']
        if skills:
            shell += [f"cat ~/.agents/skills/{name}/SKILL.md"
                      for name in ("test-driven-development", "testing", "communicating")]
        for command in shell:
            records.append({"type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": command})}})
        records.append({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Read-only diagnosis; no repository changes."}]}})
        self.tx.path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return gates.crew_gates(KID_DOCS, str(self.tx.path), str(self.repo))

    def test_external_probe_skips_only_codex_mutation_proof_and_keeps_required_skills(self):
        self.assertEqual(self.rules(self.codex([self.probe], skills=False)), [
            ("skills", "superpowers:test-driven-development"), ("skills", "testing"),
            ("skills", "communicating")])
        self.assertEqual(self.codex([self.probe]), [])
        self.tx.call("Write", file_path=str(self.probe), content=self.probe.read_text())
        self.complete()
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [("proofs", "dev test prove")])

    def test_repository_changes_still_owe_proof_regardless_of_diagnostic_report(self):
        self.assertEqual(self.codex([self.probe]), [])
        new_source = self.repo / "calc.py"
        new_source.write_text("def add(a, b):\n    return a + b\n")
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    git(self.repo, "add", "calc.py")
                    git(self.repo, "commit", "-qm", "add calculator")
                    new_source.write_text("def add(a, b):\n    return a - b\n")
                self.assertEqual(self.rules(self.codex([self.probe, new_source])),
                                 [("proofs", "dev test prove")])
        alias = self.scratch / "source.py"
        alias.symlink_to(new_source)
        self.assertEqual(self.rules(self.codex([self.probe, alias])), [("proofs", "dev test prove")])
        nested = self.scratch / "nested"
        nested.mkdir()
        git(nested, "init", "-q")
        nested_source = nested / "calc.py"
        nested_source.write_text("value = 1\n")
        self.assertEqual(self.rules(self.codex([self.probe, nested_source])), [("proofs", "dev test prove")])

    def test_shell_changes_keep_mutation_proof_and_test_audit_but_reads_owe_neither(self):
        tests = self.repo / "tests"
        tests.mkdir()
        test = tests / "test_calc.py"
        test.write_text("import unittest\n\nclass Calc(unittest.TestCase):\n"
                        "    def test_result(self):\n        self.assertTrue(True)\n")
        self.assertEqual(self.codex([self.probe], commands=["cat tests/test_calc.py"]), [])
        findings = self.codex([self.probe], commands=["printf broken > tests/test_calc.py"])
        self.assertEqual([rule for rule, _ in self.rules(findings)], ["proofs", "tests"])
        self.assertEqual(findings[0]["item"], "dev test prove")
        self.assertIn("test_result", findings[1]["evidence"])

    def native_changes(self, changes, status="completed", thread="diagnostic", inherited=(), extra=()):
        self.codex([])
        records = [json.loads(line) for line in self.tx.path.read_text().splitlines()]
        if inherited:
            records[0]["payload"].update(forked_from_id="parent", subagent_history_start_ordinal=2)
            records[1:1] = [*inherited, {"type": "event_msg", "ordinal": 2,
                                       "payload": {"type": "task_started"}}]
        records.extend(extra)
        records.append({"type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "item": {
                "type": "FileChange", "id": "exec-patch", "changes": changes,
                "status": status, "stdout": "Success. Updated the following files", "stderr": ""}}})
        self.tx.path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return gates.crew_gates(KID_DOCS, str(self.tx.path), str(self.repo))

    def test_completed_native_adds_receive_stub_and_test_audits(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1  # TODO handle other values\n")
        test = self.repo / "test_calc.py"
        test.write_text("import unittest\n\nclass Calc(unittest.TestCase):\n"
                        "    def test_result(self):\n        self.assertTrue(True)\n")
        changes = {str(path): {"type": "add", "content": path.read_text()} for path in (code, test)}
        found = self.native_changes(changes)
        self.assertEqual([f["rule"] for f in found], ["proofs", "stubs", "tests"])
        self.assertIn("calc.py", found[1]["evidence"])
        self.assertIn("test_result", found[2]["evidence"])
        self.tx.call("Write", file_path=str(code), content=code.read_text())
        self.tx.call("Write", file_path=str(test), content=test.read_text())
        self.complete()
        self.assertEqual([f["rule"] for f in self.gates(KID_DOCS)], ["proofs", "stubs", "tests"])

    def test_native_update_audits_only_lines_the_member_added(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1\n")
        git(self.repo, "add", "calc.py")
        git(self.repo, "commit", "-qm", "add source")
        code.write_text("value = 2  # TODO own unfinished value\nother = 3  # TODO another lane\n")
        found = self.native_changes({str(code): {"type": "update", "move_path": None,
            "unified_diff": "@@ -1 +1 @@\n-value = 1\n+value = 2  # TODO own unfinished value\n"}})
        self.assertEqual([f["rule"] for f in found], ["proofs", "stubs"])
        self.assertIn("own unfinished value", found[1]["evidence"])
        self.assertNotIn("another lane", found[1]["evidence"])

    def test_native_attribution_rejects_inherited_foreign_and_failed_changes(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1  # TODO unfinished value\n")
        changes = {str(code): {"type": "add", "content": code.read_text()}}
        for status in ("failed", "in_progress", None):
            self.assertEqual(self.native_changes(changes, status=status), [])
        self.assertEqual(self.native_changes(changes, thread="sibling"), [])
        inherited = {"ordinal": 1, "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": "diagnostic", "item": {
                "type": "FileChange", "changes": changes, "status": "completed"}}}
        self.assertEqual(self.native_changes({}, inherited=[inherited]), [])
        quoted = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(inherited)}]}}
        self.assertEqual(self.native_changes({}, extra=[quoted]), [])
        self.assertEqual([f["rule"] for f in self.native_changes(changes)], ["proofs", "stubs"])

    def test_native_attribution_keeps_identity_when_a_large_rollout_tail_drops_metadata(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1  # TODO unfinished value\n")
        changes = {str(code): {"type": "add", "content": code.read_text()}}
        self.native_changes(changes, inherited=[{"ordinal": 1, "type": "event_msg", "payload": {}}])
        records = [json.loads(line) for line in self.tx.path.read_text().splitlines()]
        for ordinal, record in enumerate(records):
            record["ordinal"] = ordinal
        for padding in (0, gates.MAX_BYTES + 1):
            with self.subTest(padding=padding):
                padded = records[:-1] + [{"type": "response_item", "payload": {
                    "type": "message", "role": "user", "content": "x" * padding}}] + records[-1:]
                self.tx.path.write_text("\n".join(json.dumps(record) for record in padded) + "\n")
                found = gates.crew_gates(KID_DOCS, str(self.tx.path), str(self.repo))
                self.assertEqual([f["rule"] for f in found], ["proofs", "stubs"])
                for wrong in ({"thread_id": "sibling"}, {"ordinal": 1}, {"ordinal": None}):
                    invalid = json.loads(json.dumps(padded))
                    if "thread_id" in wrong:
                        invalid[-1]["payload"].update(wrong)
                    else:
                        invalid[-1].update(wrong)
                    self.tx.path.write_text("\n".join(json.dumps(record) for record in invalid) + "\n")
                    self.assertEqual(gates._codex_written(str(self.tx.path), str(self.repo)), {})
                for boundary in (None, True, -1, "2"):
                    invalid = json.loads(json.dumps(padded))
                    invalid[0]["payload"]["subagent_history_start_ordinal"] = boundary
                    self.tx.path.write_text("\n".join(json.dumps(record) for record in invalid) + "\n")
                    self.assertEqual(gates._codex_written(str(self.tx.path), str(self.repo)), {})
                standalone = json.loads(json.dumps(padded))
                standalone[0]["payload"].pop("forked_from_id")
                standalone[0]["payload"].pop("subagent_history_start_ordinal")
                self.tx.path.write_text("\n".join(json.dumps(record) for record in standalone) + "\n")
                self.assertEqual(list(gates._codex_written(str(self.tx.path), str(self.repo))),
                                 [str(code.resolve())])

    def test_codex_uv_run_proof_counts_without_accepting_printed_or_noncommand_payloads(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1\n")
        for command in ("uv run --no-project --python 3.13 dev test prove",
                        "/usr/local/bin/uv run --python=3.13 --no-project dev test prove",
                        "uv run --no-project --python 3.13 python3 bin/dev test prove"):
            with self.subTest(command=command):
                self.assertEqual(self.codex([code], commands=[command]), [])
        for command in ("echo 'uv run --no-project --python 3.13 dev test prove'",
                        "cat <<'EOF'\nuv run --no-project --python 3.13 dev test prove\nEOF",
                        "uv run --help dev test prove", "uv run --module dev test prove",
                        "uv run --python --no-project dev test prove", "uv run --python= dev test prove",
                        "uv run --no-project --python 3.13 echo dev test prove"):
            with self.subTest(command=command):
                self.assertEqual(self.rules(self.codex([code], commands=[command])),
                                 [("proofs", "dev test prove")])
        self.tx.call("Write", file_path=str(code), content=code.read_text())
        self.complete()
        self.tx.bash("uv run --no-project --python 3.13 dev test prove")
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [("proofs", "dev test prove")])

    def test_native_attribution_accepts_large_base_instructions_within_supported_limit(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1  # TODO unfinished value\n")
        self.native_changes({str(code): {"type": "add", "content": code.read_text()}})
        records = [json.loads(line) for line in self.tx.path.read_text().splitlines()]
        records[0]["payload"]["base_instructions"] = {"text": "x" * (80 << 10)}
        self.tx.path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        found = gates.crew_gates(KID_DOCS, str(self.tx.path), str(self.repo))
        self.assertEqual([f["rule"] for f in found], ["proofs", "stubs"])
        records[0]["payload"]["base_instructions"] = {"text": "x" * (8 << 20)}
        self.tx.path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        self.assertEqual(gates._codex_written(str(self.tx.path), str(self.repo)), {})


if __name__ == "__main__":
    unittest.main()
