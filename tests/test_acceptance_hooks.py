# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests of devkit's hooks and guard, end to end on Claude Code and Codex.

Each test runs the checkout's hook entry (`dev hook <event> --agent claude|codex`) in a child process, as an agent's
harness does: its own interpreter with `-I -S`, the payload on stdin, and a sandbox HOME the test makes, so the
state, configuration and project all live below it. The background `dev` commands the hooks start are recorded
instead of run, so nothing outlives the test. Run under /usr/bin/python3, the hooks run under Python 3.9 too.

The Codex payloads are copied from the hook capture of Codex CLI 0.167.0 (2026-11-09) and the records of its
rollouts: the code-mode `exec` call that runs shell commands inside a JavaScript program, and the CommandExecution
item that records each command with its exit code under the hook's tool_use_id. The Claude Code payloads carry the
fields Claude Code sends.

Run: python3 -m unittest tests.test_acceptance_hooks -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402
import _sandbox  # noqa: E402  (stand-ins first: no hook reaches this Mac's codex, claude, brew or gh)

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import hooks as devkit_hooks  # noqa: E402

# The child process: the checkout's CLI running `dev hook <event> --agent <agent>`, with devkit's background starts
# recorded to a file (argv[4]) instead of run. With `many` (argv[5]) it reads a JSON list of payloads and runs the
# hook once for each, in the same process, printing [[exit code, output], ...]: a day of guard calls in one start.
BOOT = (
    "import contextlib, io, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from devkit import cli, hooks\n"
    "def spawn(args, logname='sync.log'):\n"
    "    with open(sys.argv[4], 'a') as f:\n"
    "        f.write(json.dumps(list(args)) + '\\n')\n"
    "hooks._spawn = spawn\n"
    "argv = ['hook', sys.argv[2], '--agent', sys.argv[3]]\n"
    "if sys.argv[5:] != ['many']:\n"
    "    sys.exit(cli.main(argv))\n"
    "outs = []\n"
    "for p in json.load(sys.stdin):\n"
    "    sys.stdin = io.StringIO(json.dumps(p))\n"
    "    buf = io.StringIO()\n"
    "    with contextlib.redirect_stdout(buf):\n"
    "        code = cli.main(argv)\n"
    "    outs.append([code, buf.getvalue()])\n"
    "print(json.dumps(outs))\n"
)
GIT_ID = {"GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com", "GIT_COMMITTER_NAME": "Ada",
          "GIT_COMMITTER_EMAIL": "ada@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
BRANCH = "fix/settings-reads"
BLOCK = (f"**[Firefly> DevKit]** api · `{BRANCH}` · Run the tests\n\n- **Done:** ran the tests.\n"
         "- **Check:** ✓ 12 tests pass\n- **Next:** nothing.")
NO_BLOCK = "I ran the tests; 12 pass."
CODEX_SID = "01a1c178-2665-7ca1-9a5d-046a95cee8bc"  # the captured Codex session
CLAUDE_SID = "3c0f2b2e-6a51-4f7e-9d1e-2f6c0b9a7d41"
SCOUT_ID = "01a1c178-64c7-7ac1-81c6-ec8b852e1772"  # the captured scout's agent_id
EXEC_ID = "exec-5f125236-dd03-45f9-8fd3-db673fc2b9b{}"  # the captured tool_use_id, numbered per call
FAILING = "python3 -c 'import nosuchmod'"
TRACEBACK = ("Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\n    import nosuchmod\n"
             "ModuleNotFoundError: No module named 'nosuchmod'\n")


def config_events(agent: str) -> dict:
    """The hook events an agent's devkit configuration registers -> {Event: `dev hook` event name}."""
    conf = ROOT / "config" / ("claude/settings.json" if agent == "claude" else "codex/hooks.json")
    out = {}
    for event, groups in json.loads(conf.read_text())["hooks"].items():
        for g in groups:
            for h in g["hooks"]:
                words = h["command"].split()
                out[event] = words[words.index("hook") + 1]
    return out


def decision(out: dict) -> str:
    """The guard's verdict in a PreToolUse output: deny, ask, or allow."""
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") or out.get("decision") or "allow"


class Sandbox(unittest.TestCase):
    """A HOME with a project, acme/api: a git repository on a work branch with one commit."""

    python = sys.executable

    def setUp(self):
        # below /var/tmp, like the run's own home: devkit treats /tmp, /var/folders and $TMPDIR as scratch, where a
        # repository is never a tracked project
        tmp = tempfile.TemporaryDirectory(dir=_hermetic.home_base())
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name)) / "home"
        self.repo = self.home / "Development" / "acme" / "api"
        self.repo.mkdir(parents=True)
        self.state = self.home / ".local" / "state" / "devkit"
        self.spawns = self.home / "spawns.jsonl"
        self.sid = CODEX_SID
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:acme/api.git")
        (self.repo / "README.md").write_text("# api\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: start")
        self.git("checkout", "-q", "-b", BRANCH)
        # the handoff repository and the project's note: without them the guard cannot tell whether the branch is
        # embargoed, and refuses what would send it to the remote (a pull request included)
        note = self.home / "Development" / ".handoff" / "projects" / "acme" / "api.md"
        note.parent.mkdir(parents=True)
        note.write_text("# acme/api\n\n## Current state\n\nWork on the settings reads.\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(note.parents[2]), check=True, capture_output=True)
        # stubs of the agents' programs first on PATH, then the run's stand-ins for brew and gh, and devkit keeps them
        # first (tests/_sandbox.py): a hook must never start the real claude or codex (with this HOME, which is not
        # signed in, codex opens its login); each agent stub records its call, and none may happen
        self.agents_called = self.home / "agents-called.log"
        stubs = self.home / "stubs"
        stubs.mkdir()
        for name in ("codex", "claude"):
            (stubs / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> '{self.agents_called}'\nexit 1\n")
            (stubs / name).chmod(0o755)
        self.addCleanup(self._no_agent_started)
        self.env = _sandbox.env({"HOME": str(self.home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                 "LANG": "en_US.UTF-8", "DEVKIT_NO_NOTIFY": "1", "DEVKIT_NO_CI_WATCH": "1",
                                 "DEVKIT_NO_AUTOSYNC": "1",  # the handoff sync work.request_sync would start, detached
                                 **GIT_ID}, stubs=[stubs])

    def _no_agent_started(self):
        called = self.agents_called.read_text() if self.agents_called.exists() else ""
        self.assertEqual(called, "", "a hook started an agent's program")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=str(self.repo), check=True, capture_output=True, text=True,
                              env=dict(os.environ, **GIT_ID)).stdout.strip()

    def _child(self, event, agent, stdin, env, *extra):
        return subprocess.run([self.python, "-I", "-S", "-c", BOOT, str(ROOT / "lib"), event, agent, str(self.spawns),
                               *extra], input=stdin, capture_output=True, text=True, cwd=str(self.repo),
                              env=dict(self.env, **(env or {})), timeout=180)

    def hook(self, event, agent, payload, env=None):
        """One hook event -> its JSON output, or {} when it printed nothing. A hook always exits 0."""
        p = self._child(event, agent, json.dumps(payload), env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.strip()
        if event == "pre-compact" and agent == "claude" and out and not out.startswith("{"):
            return {"text": out}  # Claude Code adds a PreCompact hook's plain output to the summarizer's instructions
        try:
            return json.loads(out) if out else {}
        except ValueError:
            self.fail(f"{agent} {event} printed what is not one JSON object: {out[:600]!r}")

    def hooks(self, event, agent, payloads):
        """The same hook event for each payload, in one child process -> [output or {}, ...]."""
        p = self._child(event, agent, json.dumps(payloads), None, "many")
        self.assertEqual(p.returncode, 0, p.stderr)
        runs = json.loads(p.stdout)
        self.assertEqual([code for code, _ in runs], [0] * len(payloads))
        return [json.loads(out) if out.strip() else {} for _, out in runs]

    def errors(self):
        """The errors the hooks logged (a hook fails open: an error shows only in hooks.log)."""
        f = self.state / "logs" / "hooks.log"
        return [ln for ln in (f.read_text().splitlines() if f.exists() else []) if " error" in ln]

    def audit(self, action):
        recs = [json.loads(ln) for f in sorted((self.state / "audit").glob("*.jsonl")) for ln in
                f.read_text().splitlines()]
        return [r for r in recs if r.get("action") == action]

    # ---------------------------------------------------------------- payloads as each agent sends them

    def codex(self, event, **fields):
        return dict({"cwd": str(self.repo), "hook_event_name": event, "model": "gpt-6.1-sol",
                     "permission_mode": "bypassPermissions", "session_id": self.sid,
                     "transcript_path": str(self.rollout), "turn_id": "01a1c178-28b7-7fa1-a67c-2bd8b7433d66"},
                    **fields)

    def claude(self, event, **fields):
        return dict({"cwd": str(self.repo), "hook_event_name": event, "permission_mode": "bypassPermissions",
                     "session_id": CLAUDE_SID, "transcript_path": str(self.transcript)}, **fields)

    @property
    def rollout(self):
        return self.home / ".codex" / "sessions" / "2026" / "11" / "09" / f"rollout-2026-11-09T10-00-39-{self.sid}.jsonl"

    @property
    def transcript(self):
        return self.home / ".claude" / "projects" / "-acme-api" / f"{CLAUDE_SID}.jsonl"

    def write_rollout(self, *records, version="0.167.0"):
        """A Codex rollout's opening records (session_meta naming the CLI version, the turn's start), then `records`."""
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        head = [{"type": "session_meta", "payload": {"id": self.sid, "session_id": self.sid, "cwd": str(self.repo),
                                                     "originator": "codex_exec", "cli_version": version,
                                                     "source": "exec"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in head + list(records)))

    def append_rollout(self, *records):
        with self.rollout.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records))

    @staticmethod
    def exec_call(script: str, n=1):
        """A code-mode `exec` call as Codex 0.167.0 records it: its input is a JavaScript program."""
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "status": "completed",
                                                     "call_id": f"call_{n}", "name": "exec", "input": script}}

    def command_record(self, n, cmd, code=0):
        """The CommandExecution item Codex writes when a command ends; its id is the hook's tool_use_id."""
        item = {"type": "CommandExecution", "id": EXEC_ID.format(n), "process_id": "59976",
                "command": ["/bin/zsh", "-lc", cmd], "cwd": f"file://{self.repo}",
                "parsed_cmd": [{"type": "unknown", "cmd": cmd}], "source": "unified_exec_startup",
                "status": "failed" if code else "completed", "aggregated_output": TRACEBACK if code else "ok\n",
                "exit_code": code, "duration": {"secs": 0, "nanos": 3333}}
        return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": item}}

    def start_codex_turn(self, prompt="Run the tests and fix what fails"):
        """SessionStart and UserPromptSubmit of a Codex session, as captured."""
        self.write_rollout()
        self.hook("session-start", "codex", self.codex("SessionStart", source="startup"))
        self.hook("prompt", "codex", self.codex("UserPromptSubmit", prompt=prompt))

    def start_claude_turn(self, prompt="Run the tests and fix what fails"):
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
        self.hook("session-start", "claude", self.claude("SessionStart", source="startup", model="claude-opus-5-5"))
        self.hook("prompt", "claude", self.claude("UserPromptSubmit", prompt=prompt))


# --------------------------------------------------------------------------- every registered event, both agents

class EveryRegisteredEvent(Sandbox):
    """Each event devkit registers for an agent, run through the hook entry with that agent's payload: it exits 0,
    logs no error and answers in its own event's schema; on both, the guard refuses a write that turns hooks off
    and Stop sends back a reply without the status block."""

    def payloads(self, agent):
        mk = self.codex if agent == "codex" else self.claude
        member = {"agent_id": SCOUT_ID, "agent_type": "scout"}
        bash = {"tool_name": "Bash", "tool_input": {"command": "git status --short"},
                "tool_use_id": EXEC_ID.format(1) if agent == "codex" else "toolu_01"}
        return {
            "session-start": mk("SessionStart", source="startup"),
            "prompt": mk("UserPromptSubmit", prompt="Run the tests and fix what fails in the login flow"),
            # a shell write that turns Codex hooks off: refused on both agents (the guard reads its text)
            "guard": mk("PreToolUse", **dict(bash, tool_input={"command": "printf 'hooks = false\\n' >> "
                                                                          "~/.codex/config.toml"})),
            "post-tool": mk("PostToolUse", tool_response="ok\n" if agent == "codex" else
                            {"stdout": "ok\n", "stderr": "", "interrupted": False}, **bash),
            "post-tool-failure": mk("PostToolUseFailure", error="Exit code 1", **bash),
            "subagent-start": mk("SubagentStart", **member),
            "subagent-stop": mk("SubagentStop", stop_hook_active=False, agent_transcript_path=str(self.rollout),
                                last_assistant_message="**[Firefly> DevKit · Lookup scout (Sparks)]** api · x",
                                **member),
            "pre-compact": mk("PreCompact", trigger="auto", custom_instructions=""),
            "post-compact": mk("PostCompact", trigger="auto", compact_summary="The session ran the tests."),
            "config-change": mk("ConfigChange", source="project_settings",
                                file_path=str(self.repo / ".claude" / "settings.json")),
            "instructions-loaded": mk("InstructionsLoaded", file_path=str(self.home / ".claude" / "CLAUDE.md"),
                                      memory_type="User", load_reason="session_start"),
            "stop": mk("Stop", stop_hook_active=False, last_assistant_message=NO_BLOCK),
            "session-end": mk("SessionEnd", reason="other"),
        }

    def run_all(self, agent):
        events = config_events(agent)
        self.write_rollout()
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("")
        payloads = self.payloads(agent)
        order = ["session-start", "prompt"] + [e for e in events.values() if e not in
                                                ("session-start", "prompt", "stop", "session-end")] + \
            ["stop", "session-end"]
        outs = {}
        for name in order:
            if name in events.values():
                outs[name] = self.hook(name, agent, payloads[name])
        for event, name in events.items():
            spec = outs[name].get("hookSpecificOutput")
            if spec:
                self.assertEqual(spec.get("hookEventName"), event, name)  # Codex rejects another event's schema
        self.assertEqual(self.errors(), [])
        return outs

    def test_every_claude_code_event_runs_and_refuses_or_sends_back_what_it_must(self):
        outs = self.run_all("claude")
        self.assertIn("route each to its Responsible member", outs["prompt"]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(decision(outs["guard"]), "deny")
        self.assertEqual(len(config_events("claude")), 13)  # docs/agents.md: Claude Code registers 13 events
        self.assertIn("devkit · workspace context", outs["session-start"]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(outs["stop"].get("decision"), "block")
        self.assertIn("no status block", outs["stop"]["reason"])

    def test_every_codex_event_runs_and_refuses_or_sends_back_what_it_must(self):
        outs = self.run_all("codex")
        self.assertIn("route each to its Responsible member", outs["prompt"]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(decision(outs["guard"]), "deny")
        self.assertEqual(len(config_events("codex")), 10)  # docs/agents.md: Codex registers 10 of its 14 events
        self.assertIn("devkit · workspace context", outs["session-start"]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(outs["stop"].get("decision"), "block")  # the canary runs on Codex turns too
        self.assertIn("no status block", outs["stop"]["reason"])


# --------------------------------------------------------------------------- Codex: the end-of-turn gates

class CodexStopGates(Sandbox):
    """The Stop gates read a Codex 0.167 turn: the commands its exec scripts ran, from the script's literal calls and
    from the CommandExecution records (a command built at run time shows only there)."""

    def setUp(self):
        super().setUp()
        # the testing skill, installed for Codex as its devkit plugin brings it: a rule asks only for installed skills
        self.skill = self.home / ".codex" / "plugins" / "cache" / "firefly-devkit" / "devkit" / "26.11.9" / "skills" / \
            "testing" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: testing\n---\n")

    def stop(self, reply=BLOCK):
        return self.hook("stop", "codex", self.codex("Stop", stop_hook_active=False, last_assistant_message=reply))

    def test_tests_run_inside_an_exec_script_owe_the_testing_skill(self):
        self.start_codex_turn()
        self.append_rollout(self.exec_call('text(await tools.exec_command({cmd:"uv run pytest -q tests"}));\n'),
                            self.command_record(1, "uv run pytest -q tests"))
        out = self.stop()
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("`testing`", out["reason"])

    def test_a_test_command_built_at_run_time_is_read_from_its_command_record(self):
        self.start_codex_turn()
        script = 'const tool = "pytest";\ntext(await tools.exec_command({cmd: `uv run ${tool} -q`}));\n'
        self.append_rollout(self.exec_call(script), self.command_record(1, "uv run pytest -q"))
        out = self.stop()
        self.assertIn("`testing`", out.get("reason", ""))

    def test_a_session_whose_script_read_the_skill_owes_nothing_and_another_still_does(self):
        # the twin of the two above: the same tests after the script read the testing skill (as Codex loads one)
        self.start_codex_turn()
        read = f"cat {self.skill}"
        self.append_rollout(
            self.exec_call(f"text(await tools.exec_command({{cmd:{json.dumps(read)}}}));\n"
                           'text(await tools.exec_command({cmd:"uv run pytest -q tests"}));\n'),
            self.command_record(1, read), self.command_record(2, "uv run pytest -q tests"))
        self.assertEqual(self.stop(), {})
        # a loaded skill counts for its own session only: another Codex session that runs the tests owes it
        self.sid = "01a1c178-2665-7ca1-9a5d-046a95cee8bd"
        self.start_codex_turn()
        self.append_rollout(self.exec_call('text(await tools.exec_command({cmd:"uv run pytest -q tests"}));\n'),
                            self.command_record(1, "uv run pytest -q tests"))
        self.assertIn("`testing`", self.stop().get("reason", ""))


# --------------------------------------------------------------------------- Codex: a failed command

class CodexFailedCommands(Sandbox):
    """Codex has no PostToolUseFailure: a failed command reaches PostToolUse with its output alone. devkit reads the
    exit code from the CommandExecution record whose id is the hook's tool_use_id."""

    def post(self, n):
        return self.hook("post-tool", "codex", self.codex(
            "PostToolUse", tool_name="Bash", tool_input={"command": FAILING}, tool_response=TRACEBACK,
            tool_use_id=EXEC_ID.format(n)))

    def context(self, out):
        return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def test_the_same_failure_twice_stops_the_patch_loop_only_where_codex_records_commands(self):
        self.start_codex_turn()
        for n in (1, 2):
            self.append_rollout(self.command_record(n, FAILING, code=1))
        self.post(1)
        out = self.post(2)
        self.assertIn(devkit_hooks.ROOT_CAUSE_STOP, self.context(out))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        # an older Codex writes no CommandExecution records: its calls' outcome is unknown, never a failure
        self.write_rollout(version="0.160.0")
        for n in (3, 4):
            self.assertNotIn(devkit_hooks.ROOT_CAUSE_STOP, self.context(self.post(n)))
        self.assertEqual(self.errors(), [])

    def test_a_record_written_after_the_hook_is_settled_by_the_next_call(self):
        self.start_codex_turn()
        self.append_rollout(self.command_record(1, FAILING, code=1))
        self.post(1)
        self.assertNotIn(devkit_hooks.ROOT_CAUSE_STOP, self.context(self.post(2)))  # its record is not written yet
        self.append_rollout(self.command_record(2, FAILING, code=1), self.command_record(3, FAILING, code=1))
        self.assertIn(devkit_hooks.ROOT_CAUSE_STOP, self.context(self.post(3)))  # the second, settled now, and the third


# --------------------------------------------------------------------------- Codex: whose records

class CodexIdentity(Sandbox):
    """A Codex started from a Claude Code terminal or a crew member's shell inherits their variables; its hooks'
    records carry Codex and its own session."""

    LAUNCHER = {"CLAUDECODE": "1", "DEVKIT_AGENT": "claude", "DEVKIT_SESSION": f"claude:{CLAUDE_SID}",
                "DEVKIT_FROM_AGENT": "a_tank"}

    def deny(self, **member):
        self.write_rollout()
        out = self.hook("guard", "codex", self.codex(
            "PreToolUse", tool_name="Bash", tool_input={"command": "git checkout -b codex/parity-probe"},
            tool_use_id=EXEC_ID.format(1), **member), env=self.LAUNCHER)
        self.assertEqual(decision(out), "deny")
        return self.audit("guard.deny")

    def test_a_codex_started_from_claude_code_records_its_refusals_as_its_own(self):
        denials = self.deny()
        self.assertEqual([(r["actor"]["agent"], r["actor"]["session"]) for r in denials], [("codex", CODEX_SID[:12])])
        self.assertNotIn("crew", denials[0])

    def test_a_codex_members_refusal_carries_the_main_session_and_its_own_lane(self):
        denials = self.deny(agent_id=SCOUT_ID, agent_type="scout")
        self.assertEqual([(r["actor"]["agent"], r["actor"]["session"]) for r in denials], [("codex", CODEX_SID[:12])])
        self.assertEqual(denials[0]["crew"]["lane"], SCOUT_ID)


# --------------------------------------------------------------------------- the guard on a working day

# Commands an agent runs on an ordinary day, many naming the agents' settings files in text or reading them. None may
# be refused: a false refusal stops real work. The commit messages and PR bodies name settings files on purpose.
EVERYDAY = (
    "git status --short", "git add lib/app.py tests/test_app.py", "git checkout main", "git checkout -b fix/login-timeout",
    "git restore lib/app.py", "git restore --staged tests/test_app.py", "git rebase origin/main",
    "git commit -m 'docs: explain why ~/.claude/settings.json keeps the hooks'",
    "git commit -m 'fix(codex): read the hooks table of ~/.codex/config.toml'",
    'gh pr create --draft --title "fix(codex): honor CODEX_HOME" --body "Reads $CODEX_HOME/config.toml and '
    '$CODEX_HOME/hooks.json."',
    'dev commit -m "fix(guard): allow reads of .claude/settings.local.json" --why "Reads were refused." '
    '--verified "python3 -m unittest tests.test_app (12 pass)"',
    "dev test", "dev test tests/test_app.py", "dev ci --run", 'dev progress note "suite running" --next "commit"',
    'dev progress step "guard probes" --check "40 commands allowed"', 'dev crew ask "may I change .codex/hooks.json?"',
    "npm test", "pnpm install --frozen-lockfile", "uv run pytest -q tests", "pytest -x tests/test_app.py",
    "prettier --check .", "cat ~/.claude/settings.json", "jq '.hooks.Stop' ~/.claude/settings.json",
    "grep -n disableAllHooks .claude/settings.local.json ~/.claude/settings.json",
    "cat ~/.codex/config.toml ~/.codex/hooks.json", "sed -n '1,40p' ~/.codex/config.toml",
    "diff ~/.claude/settings.json .claude/settings.json", "git diff -- .claude/settings.json",
    "git log -p -- .codex/config.toml", "cp ~/.claude/settings.json /var/tmp/settings-before.json",
    "mkdir -p .claude/agents", "touch .claude/settings.local.json", "dev setup --preview", "dev setup --help",
    "echo '{\"permissions\": {\"allow\": [\"Bash(npm test)\"]}}' > .claude/settings.local.json",
)
# The forms that turn devkit's hooks off, or leave which agents devkit sets up to an agent: refused on both agents.
SWITCHES = (
    "echo '{\"disableAllHooks\": true}' > .claude/settings.local.json",
    "printf 'hooks = false\\n' >> ~/.codex/config.toml",
    "sed -i '' 's/true/false/' ~/.claude/settings.json",
    "cp /var/tmp/hooks.json ~/.codex/hooks.json",
    "cd ~/.codex && cat <<'EOF' > config.toml\n[features]\ncodex_hooks = false\nEOF",
    "git checkout HEAD -- .claude/settings.json",
    "dev setup", "dev setup --resume", "dev config set agents.codex false",
)


class GuardWorkingDay(Sandbox):

    def day(self, agent):
        mk = self.codex if agent == "codex" else self.claude
        self.write_rollout()
        cmds = EVERYDAY + SWITCHES
        outs = self.hooks("guard", agent, [mk("PreToolUse", tool_name="Bash", tool_input={"command": c},
                                              tool_use_id=EXEC_ID.format(i)) for i, c in enumerate(cmds)])
        verdicts = dict(zip(cmds, [decision(o) for o in outs]))
        self.assertEqual({c: v for c, v in verdicts.items() if c in EVERYDAY and v != "allow"}, {})
        self.assertEqual({c: v for c, v in verdicts.items() if c in SWITCHES and v != "deny"}, {})

    def test_a_claude_code_day_runs_and_only_the_switches_are_refused(self):
        self.day("claude")

    def test_a_codex_day_runs_and_only_the_switches_are_refused(self):
        self.day("codex")


class GuardFileTools(Sandbox):
    """Write, Edit and MultiEdit (Claude Code) and apply_patch (Codex) on the agents' settings files, through the
    hook: judged by the text the file holds after the call. Each refusal has an allowed twin."""

    SETTINGS = '{\n  "disableAllHooks": false,\n  "enabledPlugins": {"devkit@firefly-devkit": true}\n}\n'

    def setUp(self):
        super().setUp()
        self.write_rollout()
        (self.repo / ".claude").mkdir()
        (self.repo / ".codex").mkdir()
        self.settings = self.repo / ".claude" / "settings.json"
        self.settings.write_text(self.SETTINGS)
        self.config = self.repo / ".codex" / "config.toml"
        self.config.write_text('[features]\nmodel = "a"\n')

    def claude_calls(self):
        f = str(self.settings)
        return [
            ("deny", "Edit", {"file_path": f, "old_string": '"disableAllHooks": false',
                              "new_string": '"disableAllHooks": true'}),
            ("allow", "Edit", {"file_path": f, "old_string": '"disableAllHooks": false',
                               "new_string": '"disableAllHooks": false, "cleanupPeriodDays": 14'}),
            ("deny", "Edit", {"file_path": f, "old_string": "true", "new_string": "false", "replace_all": True}),
            ("deny", "MultiEdit", {"file_path": f, "edits": [
                {"old_string": "false", "new_string": "0"}, {"old_string": "true", "new_string": "false"}]}),
            ("allow", "MultiEdit", {"file_path": f, "edits": [{"old_string": "false", "new_string": "false "}]}),
            ("deny", "Write", {"file_path": f, "content": '{"disableAllHooks": true}\n'}),
            ("allow", "Write", {"file_path": str(self.repo / ".claude" / "notes.json"),
                                "content": '{"disableAllHooks": true}\n'}),
        ]

    def codex_patches(self):
        c = str(self.config)
        return [
            ("deny", f"*** Begin Patch\n*** Update File: {c}\n@@\n [features]\n+hooks = false\n model = \"a\"\n"
                     "*** End Patch\n"),
            ("allow", f"*** Begin Patch\n*** Update File: {c}\n@@\n [features]\n+hooks = true\n model = \"a\"\n"
                      "*** End Patch\n"),
            ("deny", f"*** Begin Patch\n*** Add File: {self.repo / 'app' / '.codex' / 'config.toml'}\n+[features]\n"
                     "+codex_hooks = false\n*** End Patch\n"),
            ("allow", f"*** Begin Patch\n*** Add File: {self.repo / 'docs' / 'codex.toml'}\n+[features]\n"
                      "+codex_hooks = false\n*** End Patch\n"),
        ]

    def test_claude_code_file_tools_are_judged_by_the_settings_they_leave(self):
        calls = self.claude_calls()
        outs = self.hooks("guard", "claude", [self.claude("PreToolUse", tool_name=t, tool_input=i,
                                                          tool_use_id=f"toolu_{n}") for n, (_, t, i) in
                                              enumerate(calls)])
        self.assertEqual([decision(o) for o in outs], [want for want, _, _ in calls])
        self.assertEqual(self.settings.read_text(), self.SETTINGS)  # the guard judged; it wrote nothing

    def test_codex_patches_are_judged_by_the_settings_they_leave(self):
        patches = self.codex_patches()
        outs = self.hooks("guard", "codex", [self.codex("PreToolUse", tool_name="apply_patch",
                                                        tool_input={"command": body}, tool_use_id=EXEC_ID.format(n))
                                             for n, (_, body) in enumerate(patches)])
        self.assertEqual([decision(o) for o in outs], [want for want, _ in patches])


if __name__ == "__main__":
    unittest.main()
