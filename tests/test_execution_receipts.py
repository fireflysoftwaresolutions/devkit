# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Execution receipts and explicit verdicts at the crew approval boundary."""
import json
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import approvals as ap, core, gates  # noqa: E402
from tests.test_approvals import REPORT  # noqa: E402
from tests.test_crew_gates import GateCase, KID, SCOUT  # noqa: E402


class ExplicitVerdicts(unittest.TestCase):
    def test_unmarked_latest_evidence_cannot_complete_work(self):
        for check in ("UNVERIFIED: reproduction only", "tests not run", "– diagnosis", ""):
            with self.subTest(check=check):
                lane = {"updates": [{"kind": "step", "check": "✓ prior passing check"},
                                    {"kind": "step", "check": check}]}
                self.assertEqual(ap.ending(lane)[0], "unverified")

    def test_current_report_cannot_borrow_previous_success(self):
        lane = {"updates": [{"kind": "step", "check": "✓ prior passing check"}]}
        for check in ("UNVERIFIED: reproduction only", "tests not run", "– diagnosis", ""):
            with self.subTest(check=check):
                self.assertEqual(ap.ending(lane, REPORT.format(check))[0], "unverified")
        self.assertEqual(ap.ending(lane, "UNVERIFIED: no status block")[0], "unverified")


class CompletedProcedures(GateCase):
    def test_denied_and_pending_calls_do_not_prove_a_procedure(self):
        self.tx.skill("docs").refused("dev grep something")
        member = dict(SCOUT, tools="review")
        self.assertIn(("proofs", "dev grep"), self.rules(self.gates(member)))
        self.tx.bash("dev grep something")
        self.tx.records.pop()
        self.assertIn(("proofs", "dev grep"), self.rules(self.gates(member)))

    def test_background_procedure_needs_a_terminal_output_read(self):
        self.tx.skill("docs")
        self.tx.background("dev grep something", "job")
        member = dict(SCOUT, tools="review")
        self.assertIn(("proofs", "dev grep"), self.rules(self.gates(member)))
        self.tx.call("TaskOutput", "<status>completed</status><exit_code>0</exit_code>", task_id="job")
        self.assertEqual(self.gates(member), [])

    def test_completed_failed_procedure_can_support_an_honest_negative_report(self):
        self.tx.skill("docs").bash("dev grep something", "Process exited with code 1", is_error=True)
        member = dict(SCOUT, tools="review")
        self.assertEqual(self.gates(member), [])
        sess = {}
        data = {"agent_id": "reviewer", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo),
                "last_assistant_message": "**[Firefly> DevKit]** repo · `main` · Review\n"
                                          "- **Check:** ⚠ check failed\n"}
        self.assertIsNone(gates.check(member, data, sess))
        self.assertNotIn("reviewer", sess.get(gates.PASSED, {}))
        data["last_assistant_message"] = data["last_assistant_message"].replace("⚠ check failed", "✓ checked")
        self.assertEqual(gates.check(member, data, sess)["decision"], "block")
        self.assertNotIn("reviewer", sess.get(gates.PASSED, {}))

    def test_wrong_tree_and_lookalike_executable_do_not_prove_a_procedure(self):
        self.tx.skill("docs")
        member = dict(SCOUT, tools="review")
        self.tx.bash("/tmp/fake/dev grep something")
        self.tx.bash("python3 dev grep something")
        self.tx.bash("cd /tmp && dev grep something")
        self.assertIn(("proofs", "dev grep"), self.rules(self.gates(member)))

    def test_help_output_does_not_prove_the_named_procedure(self):
        self.tx.skill("docs").bash("dev grep --help")
        self.assertIn(("proofs", "dev grep"), self.rules(self.gates(dict(SCOUT, tools="review"))))


class CurrentApprovalTokens(GateCase):
    def check(self, sess, **extra):
        data = {"agent_id": "a1", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo), **extra}
        return gates.check("Kid", data, sess, "owner/repo", roster={"members": [KID]})

    def test_current_unverified_report_cannot_keep_a_previous_approval_token(self):
        self.complete()
        for mark in ("⚠ not verified", "✗ failed", "UNVERIFIED", "– diagnosis", ""):
            with self.subTest(mark=mark):
                sess = {gates.PASSED: {"a1": 5.0}}
                report = f"**[Firefly> DevKit]** repo · `main` · Build\n- **Check:** {mark}\n"
                self.assertIsNone(self.check(sess, last_assistant_message=report))
                self.assertNotIn("a1", sess.get(gates.PASSED, {}))


class CodexExecutionProofs(GateCase):
    def test_spoofed_interpreters_and_wrappers_cannot_claim_source_execution(self):
        source = shlex.quote(str(core.DEVKIT_HOME / "bin" / "dev"))
        for name, suffix in (("python3", f"{source} grep something"),
                             ("uv", f"run --no-project {source} grep something")):
            fake = Path(self.tmp.name) / name
            fake.write_text("#!/bin/sh\nprintf 'not executed\\n'\n")
            fake.chmod(0o755)
            with self.subTest(name=name):
                self.receipt_records(self.call(cmd=f"{fake} {suffix}"), self.output(exit_code=0, output="not executed"))
                self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])

    def test_custom_execution_context_cannot_borrow_host_default_resolution(self):
        for overrides in ({"shell": "/tmp/zsh"}, {"env": {"PATH": "/tmp"}}, {"environment": {"PATH": "/tmp"}}):
            with self.subTest(overrides=overrides):
                self.receipt_records(self.call(cmd="dev grep something", **overrides),
                                     self.output(exit_code=0, output="not executed"))
                self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])
        for script in ('await tools.exec_command({cmd: "dev grep something", env: {PATH: "/tmp"}})',
                       'await tools.exec_command({cmd: "dev grep something", shell: "/tmp/zsh"})',
                       'await tools.exec_command({cmd: "dev grep something", ...options})',
                       'const run = tools.exec_command; await run(options)'):
            with self.subTest(script=script):
                nested = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                    "input": script}}
                self.receipt_records(nested, self.native())
                self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])

        self.receipt_records(self.call(cmd="dev test run --json"), self.output(exit_code=0,
            output=json.dumps({"top": str(self.repo), "steps": [{"runs": [{"passed": 1, "failed": 0}]}]})))
        self.assertEqual(self.proof_findings("dev test run"), [])
        self.assertIn("own", self.stop("dev test run")[1])

    def test_test_options_do_not_hide_nonexecuting_subcommands(self):
        self.write("calc.py", "value = 1\n")
        for command in ("dev test --json plan", "dev test --json audit", "dev test --runner unittest plan",
                        "dev test --json plan --all"):
            with self.subTest(command=command):
                self.receipt_records(self.call(cmd=command), self.output(exit_code=0, output="{}"))
                self.append_code_change()
                self.assertEqual(self.rules(self.proof_findings("dev test")), [("proofs", "dev test")])
        self.receipt_records(self.call(cmd="dev test --json audit"), self.output(exit_code=0, output="{}"))
        test_body = "from calc import value\n\ndef test_value():\n    assert value == 1\n"
        self.write("tests/test_calc.py", test_body)
        with self.tx.path.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call",
                "name": "apply_patch", "input": f"*** Add File: {self.repo / 'tests/test_calc.py'}\n+" +
                test_body.replace("\n", "\n+")}}) + "\n")
        self.assertEqual([f for f in self.proof_findings("dev test audit") if f["rule"] == "proofs"], [])

    def test_project_options_cannot_prove_checks_of_another_tree(self):
        for option in ("-p OTHER", "-pOTHER", "--project OTHER", "--project=OTHER"):
            with self.subTest(option=option):
                self.receipt_records(self.call(cmd=f"dev test {option} run"), self.output(exit_code=0, output="1 passed"))
                self.assertEqual(self.rules(self.proof_findings("dev test run")), [("proofs", "dev test run")])
                self.append_code_change()
                self.assertEqual(self.rules(self.proof_findings("dev test")), [("proofs", "dev test")])
        for target in (str(self.repo), str(Path(self.tmp.name) / "other")):
            result = {"top": target, "steps": [{"runs": [{"passed": 1, "failed": 0}]}]}
            self.receipt_records(self.call(cmd="dev test --project=SELF run --json"),
                                 self.output(exit_code=0, output=json.dumps(result)))
            expected = [] if target == str(self.repo) else [("proofs", "dev test run")]
            self.assertEqual(self.rules(self.proof_findings("dev test run")), expected)

    def test_scan_and_ci_proofs_reject_other_projects_and_attestations(self):
        for command, proof in (("dev sec scan OTHER", "dev sec scan"),
                               ("dev ci --run OTHER", "dev ci --run"),
                               ("dev ci --run --project=OTHER", "dev ci --run"),
                               ("dev ci --run --mark-passed --why attested", "dev ci --run")):
            with self.subTest(command=command):
                self.receipt_records(self.call(cmd=command), self.output(exit_code=0, output="ok"))
                self.assertEqual(self.rules(self.proof_findings(proof)), [("proofs", proof)])

    def test_ci_inspection_variants_cannot_prove_executed_checks(self):
        for flag in ("--dry-run", "--evidence"):
            with self.subTest(flag=flag):
                self.receipt_records(self.call(cmd=f"dev ci --run {flag}"),
                                     self.output(exit_code=0, output="ok"))
                self.assertEqual(self.rules(self.proof_findings("dev ci --run")), [("proofs", "dev ci --run")])
        self.receipt_records(self.call(cmd="dev ci --run"), self.output(exit_code=0, output="checks passed"))
        self.assertEqual(self.proof_findings("dev ci --run"), [])

    def append_code_change(self):
        code = self.repo / "calc.py"
        code.write_text("value = 1\n")
        with self.tx.path.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call",
                "name": "apply_patch", "input": f"*** Add File: {code}\n+value = 1\n"}}) + "\n")

    def receipt_records(self, *records):
        head = {"type": "session_meta", "payload": {"id": "own", "cwd": str(self.repo)}}
        self.tx.path.write_text("\n".join(json.dumps(r) for r in (head, *records)) + "\n")

    def proof_findings(self, proof="dev grep"):
        member = {"id": "reviewer", "name": "Reviewer", "tools": "review",
                  "procedure": [{"phase": "Review", "proof": proof}]}
        return gates.crew_gates(member, str(self.tx.path), str(self.repo))

    def stop(self, proof="dev grep", mark="✓"):
        member = {"id": "reviewer", "name": "Reviewer", "tools": "review",
                  "procedure": [{"phase": "Review", "proof": proof}]}
        sess = {}
        result = gates.check(member, {"agent_id": "own", "agent_transcript_path": str(self.tx.path),
            "cwd": str(self.repo), "last_assistant_message":
            f"**[Firefly> DevKit]** repo · `main` · Review\n- **Check:** {mark} result\n"}, sess)
        return result, sess.get(gates.PASSED, {})

    def call(self, name="exec_command", call_id="call", **args):
        return {"type": "response_item", "payload": {"type": "function_call", "name": name,
            "call_id": call_id, "arguments": json.dumps(args or {"cmd": "dev grep something"})}}

    def output(self, call_id="call", **result):
        return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id,
            "output": json.dumps(result)}}

    def native(self, status="completed", code=0, thread="own"):
        return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread, "item": {
            "type": "CommandExecution", "id": "native", "status": status, "exit_code": code,
            "command": ["/bin/sh", "-lc", "dev grep something"], "cwd": str(self.repo)}}}

    def test_a_pending_session_needs_its_correlated_terminal_result(self):
        start = [self.call(), self.output(session_id=7, output="still running")]
        self.receipt_records(*start)
        self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])
        self.receipt_records(*start, self.output("other", exit_code=0))
        self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])
        wait = self.call("write_stdin", "wait", session_id=7)
        self.receipt_records(*start, wait, self.output("wait", exit_code=1, output="failed test"))
        self.assertEqual(self.proof_findings(), [])
        self.assertEqual(self.stop(mark="⚠"), (None, {}))

    def test_code_mode_requires_native_completion_not_the_script_text(self):
        script = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
            "input": 'await tools.exec_command({cmd: "dev grep something"})'}}
        for result in (self.native("in_progress", None), self.native("declined", 1), self.native(thread="foreign")):
            with self.subTest(result=result):
                self.receipt_records(script, result)
                self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])
        self.receipt_records(script)
        self.assertEqual(self.rules(self.proof_findings()), [("proofs", "dev grep")])
        self.receipt_records(script, self.native())
        self.assertEqual(self.proof_findings(), [])
        self.assertIn("own", self.stop()[1])

    def test_failed_native_completion_is_performed_but_never_successful(self):
        for code in (1, 0):
            with self.subTest(code=code):
                self.receipt_records(self.native("failed", code))
                self.assertEqual(self.proof_findings(), [])
                self.assertEqual(self.stop(mark="✗"), (None, {}))
                result, tokens = self.stop()
                self.assertIsNotNone(result)
                self.assertEqual(result["decision"], "block")
                self.assertEqual(tokens, {})

    def test_skip_only_and_partial_mutation_reports_cannot_verify_success(self):
        cases = (("dev test run", {"steps": [{"runs": [{"passed": 0, "skipped": 3, "failed": 0}]}]}),
                 ("dev test prove", {"tests": [{"verdict": "proves the change"}, {"verdict": "skipped"}]}))
        for proof, result in cases:
            with self.subTest(proof=proof):
                self.receipt_records(self.call(cmd=proof), self.output(exit_code=0, output=json.dumps(result)))
                if proof == "dev test prove":
                    self.write("calc.py", "value = 1\n")
                    records = [json.loads(line) for line in self.tx.path.read_text().splitlines()]
                    records.append({"type": "response_item", "payload": {"type": "custom_tool_call",
                        "name": "apply_patch", "input": f"*** Add File: {self.repo / 'calc.py'}\n+value = 1\n"}})
                    self.tx.path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
                self.assertEqual(self.proof_findings(proof), [])
                result, tokens = self.stop(proof)
                self.assertIsNotNone(result)
                self.assertEqual(result["decision"], "block")
                self.assertEqual(tokens, {})


if __name__ == "__main__":
    unittest.main()
