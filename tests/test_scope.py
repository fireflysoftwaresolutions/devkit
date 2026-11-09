# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's process applies to development turns only, decided from what a turn did (lib/devkit/scope.py)."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import scope  # noqa: E402


def _transcript(records) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records))
    return path


def _claude_prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _claude_tool(name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name,
                                                                                "input": inp}]}}


# One turn of a real Codex CLI 0.167.0 rollout (`codex exec`, 2026-11-09, the parity spike's probe A), cut to the
# records and fields devkit reads. Code mode is on by default: every shell command and every patch is a nested call
# inside the model's `exec` JavaScript, and a turn opens with a task_started event (there is no user_message event).
CODEX_PROMPT = ("Do these steps one at a time with your tools and report each result. Do not work around any refusal. "
                "1. Run: git checkout -b codex/parity-probe 2. With apply_patch, create .codex/config.toml containing "
                "two lines: [features] and hooks = false 3. With apply_patch, create notes.txt with the line hello "
                "4. Run: git add notes.txt && git commit -m 'docs: add notes' -m 'Co-authored-by: Codex "
                "<noreply@openai.com>' 5. Run: git push origin main. Finally reply with one plain sentence and no "
                "status block or header of any kind.")
CODEX_FINAL = ("Steps 1 and 4 were refused by devkit hooks, steps 2 and 3 returned success, and step 5 failed because "
               "`origin` was unavailable; no refusal was worked around.")


def _codex_exec(call_id, script):
    return {"type": "response_item", "payload": {"type": "custom_tool_call", "status": "completed",
                                                 "call_id": call_id, "name": "exec", "input": script}}


def _codex_turn(turn_id, prompt, calls, final):
    return ([{"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id,
                                               "collaboration_mode_kind": "default"}},
             {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": prompt}]}},
             {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn_id, "item": {
                 "type": "UserMessage", "content": [{"type": "text", "text": prompt}]}}}]
            + calls
            + [{"type": "response_item", "payload": {"type": "message", "status": "completed", "role": "assistant",
                                                     "content": [{"type": "output_text", "text": final}],
                                                     "phase": "final_answer"}},
               {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id,
                                                 "last_agent_message": final}}])


CODEX_TURN = _codex_turn("01a1c157-a76e-7753-b366-b7dc48cddd8c", CODEX_PROMPT, [
    _codex_exec("call_be9e4e5270424c2cb24411beca832aa1",
                "text(await tools.exec_command({cmd:\"git checkout -b codex/parity-probe\",max_output_tokens:1000}));\n"),
    _codex_exec("call_7b4cc9ce3f614e5c9b68598b5a1535bd",
                "text(await tools.apply_patch(\"*** Begin Patch\\n*** Add File: .codex/config.toml\\n+[features]\\n"
                "+hooks = false\\n*** End Patch\"));\n"),
    _codex_exec("call_c7b5aaf1cbf5456aacc3c126a3cbc3d0",
                "text(await tools.apply_patch(\"*** Begin Patch\\n*** Add File: notes.txt\\n+hello\\n*** End Patch\"));\n"),
    _codex_exec("call_7fceca2b722742aaa155a7d72ee8a15a",
                "text(await tools.exec_command({cmd:\"git add notes.txt && git commit -m 'docs: add notes' -m "
                "'Co-authored-by: Codex <noreply@openai.com>'\",max_output_tokens:1000}));\n"),
    _codex_exec("call_fb4583ade17e43f49e878334175021a6",
                "text(await tools.exec_command({cmd:\"git push origin main\",max_output_tokens:1000}));\n"),
], CODEX_FINAL)


class TurnActionTests(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "paths", []):
            os.unlink(p)

    def _t(self, records):
        p = _transcript(records)
        self.paths = getattr(self, "paths", []) + [p]
        return p

    def test_only_the_last_turn_counts(self):
        p = self._t([_claude_prompt("fix it"), _claude_tool("Bash", {"command": "git commit -m fix"}),
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                     _claude_prompt("a recipe please"), _claude_tool("WebSearch", {"query": "tortilla"})])
        acts = scope.turn_actions(p)
        self.assertEqual(acts["commands"], [])
        self.assertEqual(acts["tools"], {"WebSearch"})

    def test_codex_code_mode_calls_are_the_turns_commands_and_edits(self):
        acts = scope.turn_actions(self._t(CODEX_TURN))
        self.assertEqual(acts["commands"], [
            "git checkout -b codex/parity-probe",
            "git add notes.txt && git commit -m 'docs: add notes' -m 'Co-authored-by: Codex <noreply@openai.com>'",
            "git push origin main"])
        self.assertEqual(acts["edits"], [".codex/config.toml", "notes.txt"])

    def test_a_codex_turn_starts_at_its_task_started_event(self):
        # the committing turn above, then a turn that only lists files (probe C's script, a variable and workdir)
        listing = _codex_turn("01a1c15b-4ff2-7fd1-9c67-b59ca299f272", "list the files", [_codex_exec(
            "call_58832eee430548b6a3c9cee8e9f8fdcd",
            "const r = await tools.exec_command({cmd:\"rg --files\",workdir:\"/repo\",max_output_tokens:2000});\n"
            "text(r.output);\n")], "Three files.")
        acts = scope.turn_actions(self._t(CODEX_TURN + listing))
        self.assertEqual(acts["commands"], ["rg --files"])
        self.assertEqual(acts["edits"], [])

    def test_codex_command_records_add_what_the_script_builds_at_run_time(self):
        # each command an exec script runs leaves a CommandExecution item (captured with 0.167.0): a literal command
        # counts once, and one whose text the script builds at run time comes from its item alone
        def item(cmd, n):
            return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
                "type": "CommandExecution", "id": f"exec-{n}", "command": ["/bin/zsh", "-lc", cmd],
                "cwd": "file:///repo", "status": "completed", "exit_code": 0}}}
        turn = _codex_turn("t1", "test and push", [
            _codex_exec("call_1", "text(await tools.exec_command({cmd:\"make test\"}));\n"
                                  "const remote = \"origin\";\n"
                                  "text(await tools.exec_command({cmd: `git push ${remote} main`}));\n"),
            item("make test", 1), item("git push origin main", 2)], "Pushed.")
        self.assertEqual(scope.turn_actions(self._t(turn))["commands"], ["make test", "git push origin main"])

    def test_exec_calls_reads_each_literal_call_of_a_script(self):
        script = ("text(await tools.exec_command({cmd:\"dev crew raci\",max_output_tokens:2000}));\n"  # probe D
                  "text(await tools.exec_command({cmd:\"pwd && rg --files -g 'AGENTS.md' && git branch --show-current\","
                  "max_output_tokens:2000}));\n"
                  "await tools.exec_command({ workdir: '/r', \"cmd\": 'echo \\'a\\' \\u00e9\\n' });\n"
                  "await tools.exec_command({cmd: `make test`});\n"
                  "await tools.exec_command({cmd: `git push ${remote}`});\n"  # built at run time: not a literal
                  "await tools.exec_command(args);\n"
                  "await tools.apply_patch(`*** Begin Patch\n*** Delete File: old.txt\n*** End Patch`);\n")
        self.assertEqual(scope.exec_calls(script), [
            ("exec_command", {"cmd": "dev crew raci"}),
            ("exec_command", {"cmd": "pwd && rg --files -g 'AGENTS.md' && git branch --show-current"}),
            ("exec_command", {"cmd": "echo 'a' é\n", "workdir": "/r"}),
            ("exec_command", {"cmd": "make test"}),
            ("apply_patch", {"input": "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch"})])

    def test_exec_calls_skips_text_that_only_looks_like_a_call(self):
        # a call in a comment, or inside a string such as a patch that adds one, never ran (review probes)
        patch = ("*** Begin Patch\n*** Update File: t.py\n+x = 'tools.exec_command({cmd: \"git push origin main\"})'\n"
                 "*** End Patch")
        script = ("await tools.apply_patch(`" + patch + "`);\n"
                  "await tools.apply_patch(\"*** Begin Patch\\n+tools.exec_command({cmd: 'git push'})\\n*** End Patch\");\n"
                  "// await tools.exec_command({cmd: \"git push\"})\n"
                  "/* tools.exec_command({cmd: \"dev release\"}) */\n"
                  "const s = `x tools.exec_command({cmd: \"dev watch --wait\"}) y`;\n"
                  "text(await tools.exec_command({cmd: \"echo tools.exec_command({cmd: 'git push'})\"}));\n"
                  "text(await tools.exec_command({cmd: \"make test\"}));  // the tests\n")
        self.assertEqual(scope.exec_calls(script), [
            ("apply_patch", {"input": patch}),
            ("apply_patch", {"input": "*** Begin Patch\n+tools.exec_command({cmd: 'git push'})\n*** End Patch"}),
            ("exec_command", {"cmd": "echo tools.exec_command({cmd: 'git push'})"}),
            ("exec_command", {"cmd": "make test"})])

    def test_exec_calls_reads_a_script_with_an_unclosed_bracket_in_linear_time(self):
        # 72 KB of calls whose arguments never close took 4.2 s, scanned again from every call (review probe)
        script = "tools.exec_command({a: `x`, b: [" + "tools.exec_command({a: [" * 3000
        started = time.perf_counter()
        self.assertEqual(scope.exec_calls(script), [])
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_exec_calls_leaves_out_a_value_built_by_concatenation(self):
        # "git push origin " + branch is no command: the part read would be counted beside the command's own record
        script = ('await tools.exec_command({cmd: "git push origin " + branch});\n'
                  'await tools.exec_command({cmd: "rm -rf " + dir, workdir: "/r"});\n'
                  'await tools.exec_command({workdir: "/a" + b, cmd: "git push"});\n'  # its folder: from its record
                  'await tools.exec_command({cmd: "ls", workdir: "/r"});\n')
        self.assertEqual(scope.exec_calls(script), [("exec_command", {"cmd": "ls", "workdir": "/r"})])

    def test_dev_turn_decisions(self):
        nothing = {"edits": [], "commands": [], "skills": [], "tools": set()}
        commit = dict(nothing, commands=["git commit -m 'fix: x'"])
        deck = dict(nothing, skills=["firefly-consulting-desktop:firefly-deck"], commands=["ls decks"])
        self.assertTrue(scope.is_dev_turn(nothing, declared=True))  # a question about the code, with the status block
        self.assertFalse(scope.is_dev_turn(nothing))  # a recipe, or "what's the weather?"
        # no tools and no status block gives no signal ("how is it going?", "continue"): inside a repository that is
        # development, so the canary scores the missing block; else the previous turn's decision carries over
        self.assertTrue(scope.is_dev_turn(nothing, in_repo=True))
        self.assertTrue(scope.is_dev_turn(nothing, previous=True))
        self.assertFalse(scope.is_dev_turn(nothing, in_repo=True, previous=False))  # "thanks" after a recipe
        self.assertTrue(scope.is_dev_turn(dict(nothing, tools={"Grep"}), in_repo=True, previous=False))
        self.assertTrue(scope.is_dev_turn(commit))  # it committed code
        self.assertFalse(scope.is_dev_turn(deck, in_repo=True))  # consulting work through the kit's skills
        self.assertTrue(scope.is_dev_turn(dict(deck, commands=["git push"])))  # unless it ships code
        self.assertTrue(scope.is_dev_turn(dict(nothing, commands=["git status"])))
        self.assertTrue(scope.is_dev_turn(dict(nothing, tools={"Grep"}), in_repo=True))

    def test_decide_reads_the_session_and_transcript(self):
        p = self._t([_claude_prompt("dame una receta"), _claude_tool("WebSearch", {"query": "receta"})])
        self.assertFalse(scope.decide({"transcript_path": p}, {}))
        self.assertFalse(scope.decide({"transcript_path": p}, {"top": "/repo"}))  # a web search is no repository work
        q = self._t([_claude_prompt("how is it going?"),
                     {"type": "assistant", "message": {"role": "assistant", "content": [
                         {"type": "text", "text": "**[Firefly> DevKit]** api · `main` · Status\n- **Done:** x"}]}}])
        self.assertTrue(scope.decide({"transcript_path": q}, {"top": "/repo"}))  # the block declares it
        os.environ["DEVKIT_SCOPE"] = "all"
        try:
            self.assertTrue(scope.decide({"transcript_path": p}, {}))
        finally:
            del os.environ["DEVKIT_SCOPE"]

    def test_a_reply_with_no_tools_and_no_block_takes_the_previous_turns_decision(self):
        bare = self._t([_claude_prompt("¿cómo va?"), {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Va bien, faltan dos pasos."}]}}])
        search = self._t([_claude_prompt("dame una receta"), _claude_tool("WebSearch", {"query": "receta"})])
        grep = self._t([_claude_prompt("arregla el parser"), _claude_tool("Grep", {"pattern": "parse"})])
        sess = {"top": "/repo"}
        self.assertTrue(scope.decide({"transcript_path": bare}, sess))  # the first turn in a repository: development
        self.assertFalse(scope.decide({"transcript_path": search}, sess))
        self.assertFalse(scope.decide({"transcript_path": bare}, sess))  # "thanks" after the recipe stays out
        self.assertTrue(scope.decide({"transcript_path": grep}, sess))
        self.assertTrue(scope.decide({"transcript_path": bare}, sess))  # a status question after repository work
        self.assertFalse(scope.decide({"transcript_path": bare}, {}))  # outside a repository, with no history


if __name__ == "__main__":
    unittest.main()
