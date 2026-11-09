# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests of crew coordination across sessions: messages, decisions, runs, lanes and slots.

Several agent sessions share one Mac, as they do on a developer's machine: two Claude Code conversations in the same
project, a Claude Code conversation in another project, and Codex threads in the same Codex app. Each session runs in
an agent host of its own, a process whose command line is `claude …` or `codex …`, so devkit finds the session of a
`dev` command by its parent processes (progress.resolve_session) exactly as it does under the real programs. Every
hook event runs the checkout's hook entry in a child of that host, with the payload Claude Code or Codex sends, and
every shell command runs as the harness runs it: the guard hook first (which records the call in the ledger and may
rewrite a crew member's `dev` call to its lane), then the command itself in `bash -c`, then the PostToolUse hook.

The `dev` on the commands' PATH is the checkout's CLI (cli.main from lib/), started as its own process; only the
detached background starts (hooks._spawn: `dev sync`) are recorded instead of run, so nothing outlives a test. The
real claude and codex programs are stubs that record any call, and none may happen.

Run: python3 -m unittest tests.test_acceptance_crew_sessions -v (and with /usr/bin/python3, for Python 3.9)
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
import _sandbox  # noqa: E402  (the net against this Mac's real codex, claude, brew and gh)

# Runs `dev hook <event> --agent <agent>` with the payload on stdin; background starts are recorded (argv[4]).
HOOK_BOOT = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from devkit import cli, hooks\n"
    "def spawn(args, logname='sync.log'):\n"
    "    with open(sys.argv[4], 'a') as f:\n"
    "        f.write(json.dumps(list(args)) + '\\n')\n"
    "hooks._spawn = spawn\n"
    "sys.exit(cli.main(['hook', sys.argv[2], '--agent', sys.argv[3]]))\n"
)
# The `dev` the shell commands run: the checkout's CLI with its own argv, as bin/dev runs it, background starts
# recorded. sys.argv is the command's own (`dev crew answer M1 …`): the call ledger matches a command by it.
DEV_BOOT = (
    "import json, sys\n"
    "LIB, LOG = {lib!r}, {log!r}\n"
    "sys.path.insert(0, LIB)\n"
    "sys.argv = ['dev'] + sys.argv[1:]\n"
    "from devkit import cli, hooks\n"
    "def spawn(args, logname='sync.log'):\n"
    "    with open(LOG, 'a') as f:\n"
    "        f.write(json.dumps(list(args)) + '\\n')\n"
    "hooks._spawn = spawn\n"
    "sys.exit(cli.main(sys.argv[1:]))\n"
)
# An agent's process: it starts the hooks and the shell commands of its sessions as its children, several at once
# when asked (one thread per request), and answers each request with its id.
HOST = (
    "import json, subprocess, sys, threading\n"
    "lock, threads = threading.Lock(), []\n"
    "def run(q):\n"
    "    try:\n"
    "        p = subprocess.run(q['argv'], input=q.get('input'), capture_output=True, text=True, cwd=q['cwd'],\n"
    "                           env=q['env'], timeout=q.get('timeout', 120))\n"
    "        r = {'id': q['id'], 'rc': p.returncode, 'out': p.stdout, 'err': p.stderr}\n"
    "    except subprocess.TimeoutExpired:\n"
    "        r = {'id': q['id'], 'rc': -9, 'out': '', 'err': 'timed out'}\n"
    "    with lock:\n"
    "        sys.stdout.write(json.dumps(r) + '\\n')\n"
    "        sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    t = threading.Thread(target=run, args=(json.loads(line),))\n"
    "    t.start()\n"
    "    threads.append(t)\n"
    "for t in threads:\n"
    "    t.join()\n"
)
GIT_ID = {"GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com", "GIT_COMMITTER_NAME": "Ada",
          "GIT_COMMITTER_EMAIL": "ada@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
# made-up session ids, never a real session of this Mac. The Codex ones are UUIDv7 like Codex's own: two threads
# started within the same minute share their first 8 characters.
S1 = "5e1f0a2b-7c3d-4e5f-8a9b-0c1d2e3f4a5b"
S2 = "c0ffee00-1234-4abc-8def-001122334455"
S3 = "9448c4d0-aaaa-4bbb-8ccc-0123456789ab"
CX1 = "01a1c178-2665-7ca1-9a5d-046a95cee8bc"
CX2 = "01a1c178-64c7-7ac1-81c6-ec8b852e1772"


class Host:
    """An agent's process (`claude …` or `codex …` on its command line) that runs requests as its children. The
    program is a shell started under the agent's name, and the requests run below the Python loop it starts: a
    framework Python re-executes itself under its own path, so a Python process never shows the agent's name."""

    def __init__(self, test, name: str, env: dict):
        self.env = env
        home = Path(env["HOME"])
        script, loop = home / "agent-host.sh", home / "agent-host.py"
        if not script.exists():
            script.write_text('"$1" -I -S "$2"\n')  # no exec: the agent's process stays the parent
            loop.write_text(HOST)
        self.proc = subprocess.Popen([name, str(script), sys.executable, str(loop)], executable="/bin/bash",
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
                                     cwd=env["HOME"])
        self.pending, self.cond, self.n, self.writing = {}, threading.Condition(), 0, threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        test.addCleanup(self.close)

    def _read(self):
        for line in self.proc.stdout:
            r = json.loads(line)
            with self.cond:
                self.pending[r["id"]] = r
                self.cond.notify_all()

    def send(self, argv, cwd, env=None, stdin=None, timeout=120) -> int:
        with self.cond:
            self.n += 1
            rid = self.n
        q = {"id": rid, "argv": [str(a) for a in argv], "cwd": str(cwd), "env": dict(self.env, **(env or {})),
             "input": stdin, "timeout": timeout}
        with self.writing:
            self.proc.stdin.write(json.dumps(q) + "\n")
            self.proc.stdin.flush()
        return rid

    def wait(self, rid, timeout=180) -> dict:
        end = time.time() + timeout
        with self.cond:
            while rid not in self.pending:
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"the agent host gave no answer to request {rid} in {timeout} s")
                self.cond.wait(left)
            return self.pending.pop(rid)

    def run(self, argv, cwd, env=None, stdin=None, timeout=120) -> dict:
        return self.wait(self.send(argv, cwd, env, stdin, timeout), timeout + 30)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc.stdout.close()


class Session:
    """One agent session (a Claude Code conversation or a Codex thread) in a host, in a repository."""

    def __init__(self, mac, host: Host, agent: str, sid: str, repo: Path):
        self.mac, self.host, self.agent, self.sid, self.repo = mac, host, agent, sid, repo
        self.calls, self.env_file = 0, mac.home / f"env-{sid}.sh"
        self.exports = {}

    # ---------------------------------------------------------------- hook events, as the agent sends them

    @property
    def transcript(self) -> Path:
        if self.agent == "claude":
            return self.mac.home / ".claude" / "projects" / f"-{self.repo.name}" / f"{self.sid}.jsonl"
        return self.mac.home / ".codex" / "sessions" / "2026" / "11" / "09" / f"rollout-2026-11-09T10-00-39-{self.sid}.jsonl"

    def payload(self, event: str, **fields) -> dict:
        base = {"cwd": str(self.repo), "hook_event_name": event, "session_id": self.sid,
                "transcript_path": str(self.transcript), "permission_mode": "bypassPermissions"}
        if self.agent == "codex":
            base.update(model="gpt-6.1-sol", turn_id="01a1c178-28b7-7fa1-a67c-2bd8b7433d66")
        return dict(base, **fields)

    def hook(self, event: str, payload: dict, wait=True):
        env = {"CLAUDE_ENV_FILE": str(self.env_file)} if self.agent == "claude" and event == "session-start" else {}
        rid = self.host.send([sys.executable, "-I", "-S", "-c", HOOK_BOOT, ROOT / "lib", event, self.agent,
                              self.mac.spawns], self.repo, env=env, stdin=json.dumps(payload))
        return self._hook_result(rid) if wait else rid

    def _hook_result(self, rid) -> dict:
        r = self.host.wait(rid)
        if r["rc"] != 0:
            raise AssertionError(f"the hook failed: {r['err'][-2000:]}")
        out = r["out"].strip()
        return json.loads(out) if out.startswith("{") else ({"text": out} if out else {})

    def start(self, prompt="Lead the crew on this change"):
        """SessionStart, then the developer's first prompt (UserPromptSubmit), as the agent sends them."""
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        if not self.transcript.exists():
            self.transcript.write_text("")
        out = self.hook("session-start", self.payload("SessionStart", source="startup"))
        if self.agent == "claude":  # Claude Code exports what the hook wrote to CLAUDE_ENV_FILE to every shell
            for ln in self.env_file.read_text().splitlines() if self.env_file.exists() else []:
                k, _, v = ln.replace("export ", "", 1).partition("=")
                self.exports[k] = v
        first = self.prompt(prompt)
        # what the session was told when it started: the workspace context, the welcome and the first prompt's context
        self.welcome = "\n".join([self.context(out), (out.get("systemMessage") or ""), self.context(first)])
        return first

    def prompt(self, text: str, prompt_id: str = "") -> dict:
        """The developer types `text`: the transcript records it (origin human), then UserPromptSubmit runs."""
        pid = prompt_id or f"prompt-{self.sid[:8]}-{time.time_ns()}"
        if self.agent == "claude":
            with self.transcript.open("a") as fh:
                fh.write(json.dumps({"type": "user", "promptId": pid, "origin": {"kind": "human"},
                                     "uuid": pid, "sessionId": self.sid,
                                     "message": {"role": "user", "content": text}}) + "\n")
        return self.hook("prompt", self.payload("UserPromptSubmit", prompt=text, prompt_id=pid))

    def context(self, out: dict) -> str:
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    # ---------------------------------------------------------------- shell calls, as the harness runs them

    def _ids(self):
        self.calls += 1
        n = f"{self.sid[:8]}-{self.calls}"
        return f"toolu_{n}" if self.agent == "claude" else f"exec-{n}"

    def begin(self, cmd: str, member: str = None, member_type: str = "", raw=False) -> dict:
        """The start of one Bash call of the main session (member None) or of a crew member (its agent_id): the
        guard hook, which opens the call in the ledger and may refuse it or rewrite it. -> the call, with `ran`, the
        command the shell runs (its updatedInput when it rewrites it; as typed with `raw`, a harness that drops
        updatedInput), or `denied`, the refusal."""
        extra = {"agent_id": member, "agent_type": member_type or "general-purpose"} if member else {}
        call = dict(tool_name="Bash", tool_input={"command": cmd}, tool_use_id=self._ids(), **extra)
        g = self.hook("guard", self.payload("PreToolUse", **call))
        spec = g.get("hookSpecificOutput") or {}
        if spec.get("permissionDecision") == "deny" or g.get("decision") == "block":
            return {"call": call, "denied": spec.get("permissionDecisionReason") or g.get("reason") or "?", "ran": cmd}
        return {"call": call, "denied": "", "ran": cmd if raw else ((spec.get("updatedInput") or {}).get("command")
                                                                    or cmd)}

    def execute(self, c: dict) -> dict:
        """The shell runs a begun call's command, with what the agent exports to its shells."""
        env = dict(self.exports)
        if self.agent == "codex":
            env["CODEX_THREAD_ID"] = self.sid  # Codex names the thread to the commands it runs
        r = self.host.run(["/bin/bash", "-c", c["ran"]], self.repo, env=env, timeout=90)
        c.update(rc=r["rc"], out=r["out"], err=r["err"])
        return c

    def finish(self, c: dict) -> dict:
        """The call's result: PostToolUse, which closes it in the ledger and delivers what waits for the caller."""
        resp = (c["out"] + c["err"]) if self.agent == "codex" else \
            {"stdout": c["out"], "stderr": c["err"], "interrupted": False}
        c["context"] = self.context(self.hook("post-tool", self.payload("PostToolUse", tool_response=resp,
                                                                        **c["call"])))
        return c

    def sh(self, cmd: str, member: str = None, member_type: str = "", raw=False) -> dict:
        """One whole Bash call -> {rc, out, err, denied, ran, context (what PostToolUse delivered)}."""
        c = self.begin(cmd, member, member_type, raw)
        if c["denied"]:
            return dict(c, rc=None, out="", err="", context="")
        return self.finish(self.execute(c))

    def dev(self, *args, member: str = None, member_type: str = "") -> dict:
        return self.sh(" ".join(["dev"] + [shlex.quote(str(a)) for a in args]), member, member_type)

    def ok(self, *args, member: str = None, member_type: str = "") -> str:
        r = self.dev(*args, member=member, member_type=member_type)
        if r["denied"] or r["rc"] != 0:
            raise AssertionError(f"`dev {' '.join(map(str, args))}` in {self.agent}:{self.sid[:8]} "
                                 f"{'was refused: ' + r['denied'] if r['denied'] else 'failed'}: {r['err'][-1500:]}"
                                 f"{r['out'][-800:]}")
        return r["out"]

    # ---------------------------------------------------------------- crew members

    def launch(self, aid: str, member: str, label: str):
        """The tech lead launches a crew member: the Agent call's PreToolUse, SubagentStart, and the call's result
        naming the agent it started (Claude Code); Codex fires no hook for spawn_agent, only SubagentStart."""
        kind = f"devkit:{member}" if self.agent == "claude" else member
        if self.agent == "claude":
            tid = self._ids()
            call = dict(tool_name="Agent", tool_use_id=tid,
                        tool_input={"description": label, "subagent_type": kind, "prompt": f"{label}. Do the work."})
            g = self.hook("guard", self.payload("PreToolUse", **call))
            spec = g.get("hookSpecificOutput") or {}
            if spec.get("permissionDecision") == "deny":
                return spec.get("permissionDecisionReason") or "denied"
        self.hook("subagent-start", self.payload("SubagentStart", agent_id=aid, agent_type=kind))
        if self.agent == "claude":
            self.hook("post-tool", self.payload("PostToolUse", tool_response={"agentId": aid, "status": "completed"},
                                                **call))
        return ""

    def task_stop(self, aid: str):
        """The tech lead stops a member with TaskStop: its result ends the member's lane (Claude Code fires no
        SubagentStop for it)."""
        call = dict(tool_name="TaskStop", tool_use_id=self._ids(), tool_input={"task_id": aid})
        return self.hook("post-tool", self.payload("PostToolUse", tool_response={
            "task_id": aid, "task_type": "local_agent", "message": f"Successfully stopped task: {aid}"}, **call))

    def stop(self, text: str) -> dict:
        """The turn ends with this final message (Stop): the decisions it names are shown to the developer."""
        return self.hook("stop", self.payload("Stop", stop_hook_active=False, last_assistant_message=text))

    # ---------------------------------------------------------------- devkit's records

    def run_doc(self) -> dict:
        return json.loads((self.mac.state / "runs" / f"{self.agent}-{self.sid}.json").read_text())

    def has_run(self) -> bool:
        return (self.mac.state / "runs" / f"{self.agent}-{self.sid}.json").exists()

    def messages(self) -> list:
        return [m for m in self.run_doc().get("messages") or []]

    def decision(self, did) -> dict:
        return next(d for d in self.run_doc().get("decisions") or [] if d["id"] == did)

    def step_state(self, name) -> str:
        return next(s["state"] for s in self.run_doc()["plan"] if s["name"] == name)


class Mac(unittest.TestCase):
    """A sandbox HOME with two projects, acme/api and firefly/kit-consulting, each a git repository on a work branch,
    and the handoff repository with their notes. Sessions are added by the tests."""

    python = sys.executable

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=_hermetic.home_base())  # outside the guard's scratch roots
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name)) / "home"
        self.state = self.home / ".local" / "state" / "devkit"
        self.spawns = self.home / "spawns.jsonl"
        self.api = self.repo("acme", "api", "feat/invoice-export")
        self.kit = self.repo("firefly", "kit-consulting", "feat/proposal-pages")
        handoff = self.home / "Development" / ".handoff"
        for owner, name in (("acme", "api"), ("firefly", "kit-consulting")):
            note = handoff / "projects" / owner / f"{name}.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(f"# {owner}/{name}\n\n## Current state\n\nWork in progress.\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(handoff), check=True, capture_output=True)
        # the agents' programs are stubs that record any call: a test never starts the real claude or codex
        self.agents_called = self.home / "agents-called.log"
        stubs, bin_ = self.home / "stubs", self.home / "bin"
        stubs.mkdir()
        bin_.mkdir()
        for name in ("codex", "claude"):
            (stubs / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> '{self.agents_called}'\nexit 1\n")
            (stubs / name).chmod(0o755)
        boot = self.home / "devboot.py"
        boot.write_text(DEV_BOOT.format(lib=str(ROOT / "lib"), log=str(self.spawns)))
        (bin_ / "dev").write_text(f"#!/bin/sh\nexec '{self.python}' -I -S '{boot}' \"$@\"\n")
        (bin_ / "dev").chmod(0o755)
        self.addCleanup(self._no_agent_started)
        # the shared net (tests/_sandbox.py) keeps the run's stand-ins first on PATH in every process devkit starts
        self.env = _sandbox.env({"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
                                 "DEVKIT_NO_NOTIFY": "1", "DEVKIT_NO_CI_WATCH": "1", "DEVKIT_NO_AUTOSYNC": "1",
                                 **GIT_ID}, stubs=[bin_, stubs])
        self.hosts = {}

    def _no_agent_started(self):
        # `dev status` asks the programs for their version, sign-in state and Codex's usage (`codex app-server`):
        # read-only probes the stubs answer; anything else would have started an agent
        called = self.agents_called.read_text().splitlines() if self.agents_called.exists() else []
        probes = {"claude --version", "claude auth status", "codex --version", "codex login status",
                  "codex app-server"}
        self.assertEqual([c for c in called if c not in probes], [], "a hook or a command started an agent's program")

    def repo(self, owner, name, branch) -> Path:
        r = self.home / "Development" / owner / name
        r.mkdir(parents=True)
        g = dict(os.environ, **GIT_ID)
        for args in (["init", "-q", "-b", "main"], ["remote", "add", "origin", f"git@github.com:{owner}/{name}.git"]):
            subprocess.run(["git", *args], cwd=str(r), check=True, capture_output=True, env=g)
        (r / "README.md").write_text(f"# {name}\n")
        for args in (["add", "-A"], ["commit", "-q", "-m", "chore: start"], ["checkout", "-q", "-b", branch]):
            subprocess.run(["git", *args], cwd=str(r), check=True, capture_output=True, env=g)
        return r

    def host(self, name: str, program: str = "claude") -> Host:
        if name not in self.hosts:
            self.hosts[name] = Host(self, program, self.env)
        return self.hosts[name]

    def claude(self, sid: str, repo: Path, prompt="Lead the crew on this change") -> Session:
        """A Claude Code conversation: a process of its own."""
        s = Session(self, self.host(f"claude-{sid}"), "claude", sid, repo)
        s.start(prompt)
        return s

    def codex(self, sid: str, repo: Path, app="codex-app", prompt="Lead the crew on this change") -> Session:
        """A Codex thread: the threads of one Codex app share its process."""
        s = Session(self, self.host(app, "codex"), "codex", sid, repo)
        s.start(prompt)
        return s

    def audit(self, action) -> list:
        recs = [json.loads(ln) for f in sorted((self.state / "audit").glob("*.jsonl"))
                for ln in f.read_text().splitlines() if ln.strip()]
        return [r for r in recs if r.get("action") == action]

    def errors(self) -> list:
        f = self.state / "logs" / "hooks.log"
        return [ln for ln in (f.read_text().splitlines() if f.exists() else []) if " error" in ln]


class Crew(Mac):
    """Helpers for runs with crew members."""

    def config(self, **progress):
        """The developer's devkit configuration (dev config set writes the same file)."""
        f = self.home / ".config" / "devkit" / "config.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"progress": progress}))

    def staff(self, s: Session, goal: str, plan: str, *crew):
        """A run for `goal`, and each (agent_id, member id, name, step, activity) assigned to its step and launched,
        so the step runs in that member's lane."""
        s.ok("progress", "start", goal, "--plan", plan)
        for aid, member, name, step, activity in crew:
            s.ok("progress", "assign", step, "--to", name, "--scope", f"{step}/**", "--activity", activity)
            refused = s.launch(aid, member, f"{name}: {step}")
            self.assertEqual(refused, "", refused)

    def member_hears(self, s: Session, aid: str, member: str) -> str:
        """What a member's next tool call brings it (PostToolUse delivers its messages)."""
        return s.sh("git status --short", member=aid, member_type=f"devkit:{member}")["context"]

    def assert_waiting(self, s: Session, *ids):
        run = s.run_doc()
        replied = {m.get("reply_to") for m in run.get("messages") or [] if m.get("reply_to")}
        waiting = [m["id"] for m in run.get("messages") or [] if m.get("kind") == "question" and m.get("to") == "main"
                   and not m.get("answered") and not m.get("closed") and m["id"] not in replied]
        self.assertEqual(waiting, list(ids))


# --------------------------------------------------------------------------- crew messages

class MessagesAcrossSessions(Crew):
    """Two Claude Code conversations in acme/api, each with its own run and crew, and a third in kit-consulting. Ids
    are numbered per run, so both acme/api runs hold an M1."""

    def setUp(self):
        super().setUp()
        self.a = self.claude(S1, self.api)
        self.b = self.claude(S2, self.api)
        self.staff(self.a, "Invoice export", "api=30m [backend]; review=20m after api [review]",
                   ("a1", "tank", "Tank", "api", "backend"))
        self.staff(self.b, "Release 1.2", "build=30m [release]; notes=20m after build [docs]",
                   ("b1", "link", "Link", "build", "release"))
        self.a.ok("crew", "ask", "Ship the parser with the old error format?", member="a1", member_type="devkit:tank")
        self.b.ok("crew", "ask", "May I push the release branch?", member="b1", member_type="devkit:link")

    def test_each_members_question_reaches_only_its_own_tech_lead(self):
        k = self.claude(S3, self.kit)
        k.ok("progress", "start", "Proposal pages", "--plan", "pages=1h")
        self.assertEqual([(m["id"], m["from"]) for m in self.a.messages()], [("M1", "a1")])
        self.assertEqual([(m["id"], m["from"]) for m in self.b.messages()], [("M1", "b1")])
        mine, theirs, other = (s.ok("crew", "inbox") for s in (self.a, self.b, k))
        self.assertIn("M1 from Backend engineer (Tank): Ship the parser with the old error format?", mine)
        self.assertNotIn("May I push", mine)
        self.assertIn("M1 from Release engineer (Link): May I push the release branch?", theirs)
        self.assertNotIn("old error format", theirs)
        self.assertIn("no new messages", other)
        self.assertEqual(self.errors(), [])

    def test_a_bare_answer_answers_the_callers_own_question_and_the_other_runs_m1_still_waits(self):
        self.b.ok("crew", "answer", "M1", "yes, push it")
        self.assert_waiting(self.b)
        self.assert_waiting(self.a, "M1")
        self.assertIn("yes, push it", self.member_hears(self.b, "b1", "link"))
        self.assertNotIn("yes, push it", self.member_hears(self.a, "a1", "tank"))

    def test_a_qualified_answer_from_another_session_reaches_that_runs_member(self):
        status = self.b.ok("status")
        self.assertIn('dev crew answer M1@5e1f0a2b "…"', status)  # another run's question carries its session
        self.assertIn('dev crew answer M1 "…"', status)  # the caller's own keeps its bare id
        before = self.b.messages()
        self.b.ok("crew", "answer", "M1@5e1f0a2b", "keep the old format")
        self.assert_waiting(self.a)
        self.assert_waiting(self.b, "M1")
        self.assertEqual(self.b.messages(), before)
        self.assertIn("keep the old format", self.member_hears(self.a, "a1", "tank"))
        self.assertNotIn("keep the old format", self.member_hears(self.b, "b1", "link"))

    def test_a_member_answers_only_the_messages_of_its_own_run(self):
        r = self.b.dev("crew", "answer", "M1@5e1f0a2b", "ship it", member="b1", member_type="devkit:link")
        self.assertEqual(r["rc"], 2, r)
        self.assertIn("a crew member answers the messages of its own run only", r["err"])
        self.assert_waiting(self.a, "M1")
        self.assertNotIn("ship it", self.member_hears(self.a, "a1", "tank"))

    def test_a_question_to_a_named_member_reaches_that_member_in_its_own_run_only(self):
        for s, aid, step in ((self.a, "a2", "review"), (self.b, "b2", "notes")):
            s.ok("progress", "assign", step, "--to", "Seraph", "--scope", f"{step}/**", "--activity", "review")
            self.assertEqual(s.launch(aid, "seraph", f"Seraph: {step}"), "")
        self.a.ok("crew", "ask", "--to", "Seraph", "Is the error format part of the contract?", member="a1",
                  member_type="devkit:tank")
        self.assertIn("Is the error format part of the contract?", self.member_hears(self.a, "a2", "seraph"))
        self.assertNotIn("error format", self.member_hears(self.b, "b2", "seraph"))
        self.assertNotIn("error format", json.dumps(self.b.messages()))

    def test_questions_asked_at_once_in_two_sessions_each_land_once_in_their_own_run(self):
        self.config(max_parallel=10, reserve=0)
        crew = ("dozer", "ghost", "trinity")
        for i, m in enumerate(crew):
            for s, p in ((self.a, "a"), (self.b, "b")):
                s.ok("progress", "add", f"{p}part{i}=20m [{ {'dozer': 'database', 'ghost': 'docs'}.get(m, 'frontend')}]")
                s.ok("progress", "assign", f"{p}part{i}", "--to", m.title(), "--scope", f"{p}part{i}/**")
                self.assertEqual(s.launch(f"{p}{i + 2}", m, f"{m.title()}: {p}part{i}"), "")
        jobs = [(s, f"{p}{i + 2}", m) for i, m in enumerate(crew) for s, p in ((self.a, "a"), (self.b, "b"))]
        out = {}

        def ask(s, aid, m):
            out[aid] = s.dev("crew", "ask", f"question from {aid}", member=aid, member_type=f"devkit:{m}")
        threads = [threading.Thread(target=ask, args=j) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(240)
        self.assertEqual(sorted((k, v["rc"]) for k, v in out.items()), [(j[1], 0) for j in sorted(jobs, key=lambda j: j[1])])
        for s, p in ((self.a, "a"), (self.b, "b")):
            msgs = s.messages()
            self.assertEqual(sorted(m["id"] for m in msgs), ["M1", "M2", "M3", "M4"])  # unique ids, none lost
            self.assertEqual(sorted(m["text"] for m in msgs[1:]), [f"question from {p}{i + 2}" for i in range(3)])
            self.assertEqual({m["from"] for m in msgs[1:]}, {f"{p}{i + 2}" for i in range(3)})
        self.assertEqual(self.errors(), [])

    def test_a_session_of_another_project_is_told_nothing_of_the_waiting_questions(self):
        k = self.claude(S3, self.kit)
        k.ok("progress", "start", "Proposal pages", "--plan", "pages=1h")
        seen = k.welcome + k.ok("status") + k.sh("git status --short")["context"] + k.ok("crew", "inbox")
        for text in ("old error format", "May I push", "M1@"):
            self.assertNotIn(text, seen)
        later = self.claude("7a7a7a7a-1111-4222-8333-444455556666", self.api)
        told = later.welcome + later.ok("status")
        self.assertNotIn("dev crew answer M1 ", told)  # never as its own question
        self.assertIn("M1@5e1f0a2b", told)  # the project's other runs name their session
        self.assertIn("M1@c0ffee00", told)

    def test_a_stopped_members_question_is_moot_and_a_message_to_it_waits_for_its_next_launch(self):
        self.assertIn('dev crew answer M1 "…"', self.a.ok("status"))
        self.a.task_stop("a1")
        self.assertNotIn('dev crew answer M1 "…"', self.a.ok("status"))  # nobody waits on it any more
        self.assertNotIn("M1@5e1f0a2b", self.b.ok("status"))
        sent = self.a.dev("crew", "ask", "--to", "Tank", "Use the new error codes when you resume")
        self.assertIn("M2 to Backend engineer (Tank)", sent["out"])
        self.assertIn("M2 waits for their next launch", sent["context"])  # the sender hears nobody reads it now
        # Tank works in the other session too: that lane never gets the message
        self.b.ok("progress", "add", "migrate=20m [backend]")
        self.b.ok("progress", "assign", "migrate", "--to", "Tank", "--scope", "migrate/**")
        self.assertEqual(self.b.launch("b9", "tank", "Tank: migrate"), "")
        self.assertNotIn("new error codes", self.member_hears(self.b, "b9", "tank"))
        self.a.ok("progress", "assign", "api", "--to", "Tank", "--scope", "api/**", "--activity", "backend")
        self.assertEqual(self.a.launch("a9", "tank", "Tank: api"), "")
        self.assertIn("Use the new error codes when you resume", self.member_hears(self.a, "a9", "tank"))


# --------------------------------------------------------------------------- decisions

class DecisionsAcrossSessions(Crew):
    """A pending D1 in each acme/api run: run S1's from its Backend engineer, run S2's from its Release engineer."""

    def setUp(self):
        super().setUp()
        self.a = self.claude(S1, self.api)
        self.b = self.claude(S2, self.api)
        self.staff(self.a, "Invoice export", "api=30m [backend]; review=20m after api [review]",
                   ("a1", "tank", "Tank", "api", "backend"))
        self.staff(self.b, "Release 1.2", "build=30m [release]; notes=20m after build [docs]",
                   ("b1", "link", "Link", "build", "release"))
        self.a.ok("crew", "ask", "--decision", "Keep the old error format?", "--options", "A: keep | B: drop",
                  "--recommend", "A", member="a1", member_type="devkit:tank")
        self.b.ok("crew", "ask", "--decision", "Tag on Friday?", "--options", "A: Friday | B: Monday",
                  "--recommend", "A", member="b1", member_type="devkit:link")

    def test_each_session_records_the_answer_to_its_own_decision_only(self):
        self.assertEqual((self.a.step_state("api"), self.b.step_state("build")), ("blocked", "blocked"))
        r = self.b.dev("crew", "decide", "D1@5e1f0a2b", "A: keep")
        self.assertEqual(r["rc"], 2, r)
        self.assertIn("is a decision of another session's run", r["err"])
        self.b.ok("crew", "decide", "D1", "B: Monday")
        self.assertEqual((self.b.decision("D1")["state"], self.b.decision("D1")["answer"]), ("decided", "B: Monday"))
        self.assertEqual(self.a.decision("D1")["state"], "pending")
        self.assertEqual((self.a.step_state("api"), self.b.step_state("build")), ("blocked", "running"))
        self.assertIn("Decision D1", self.member_hears(self.b, "b1", "link"))
        self.assertNotIn("Decision D1", self.member_hears(self.a, "a1", "tank"))

    def test_an_approve_line_is_the_developers_answer_only_in_the_session_it_names(self):
        for s in (self.a, self.b):
            s.stop("**[Firefly> DevKit]** api · `feat/invoice-export` · Work\n- **Done:** asked.\n- **Check:** – a "
                   "question\n- **You:** decide D1 (below)\n- **Next:** your answer to D1.")
        self.b.prompt("approve D1@5e1f0a2b: A")  # typed in the wrong session: evidence nowhere
        self.a.prompt("approve D1@5e1f0a2b: A")  # typed where it belongs
        human = {s.sid: json.loads((self.state / "sessions" / f"claude-{s.sid}.json").read_text()).get("human") or []
                 for s in (self.a, self.b)}
        self.assertEqual([e["covers"] for e in human[S2]], [])
        self.assertEqual([e["covers"] for e in human[S1]], ["D1"])
        self.assertIn("decided by the developer", self.a.ok("crew", "decide", "D1", "A: keep"))
        self.assertIn("recorded by the tech lead, not verified by the developer",
                      self.b.ok("crew", "decide", "D1", "A: Friday"))
        self.assertEqual((self.a.decision("D1")["verified"], self.b.decision("D1")["verified"]), (True, False))

    def test_the_you_line_and_the_reminders_name_only_the_sessions_own_decision(self):
        mine = self.b.ok("progress", "note", "waiting for the developer")
        you = [ln for ln in mine.splitlines() if ln.startswith("- **You:**")]
        self.assertEqual(len(you), 1, mine)
        self.assertIn("Tag on Friday?", you[0])
        self.assertNotIn("old error format", mine)
        told = self.b.prompt("how is it going?")
        self.assertNotIn("old error format", json.dumps(told))
        self.assertNotIn("old error format", self.b.ok("crew", "decisions", "--pending"))
        k = self.claude(S3, self.kit)
        k.ok("progress", "start", "Proposal pages", "--plan", "pages=1h")
        theirs = k.ok("progress", "note", "drafting")
        self.assertNotIn("**You:**", theirs)
        self.assertNotIn("Tag on Friday", k.welcome + theirs)

    def test_a_final_message_that_names_another_sessions_decision_shows_none_of_its_own(self):
        self.b.stop("**[Firefly> DevKit]** api · `feat/invoice-export` · Status\n- **Done:** read the status.\n"
                    "- **Check:** – a status\n- **Next:** answer D1@5e1f0a2b in the chat with that session.")
        self.assertIsNone(self.b.decision("D1").get("shown"))  # run S2's own D1 was never in front of the developer

    def test_withdraw_closes_only_the_callers_decision(self):
        r = self.b.dev("crew", "withdraw", "D1@5e1f0a2b", "--why", "the plan changed")
        self.assertEqual(r["rc"], 2, r)
        self.assertIn("is a decision of another session's run", r["err"])
        self.a.ok("crew", "withdraw", "D1", "--why", "the format is out of scope now")
        self.assertEqual((self.a.decision("D1")["state"], self.b.decision("D1")["state"]), ("withdrawn", "pending"))
        self.assertEqual((self.a.step_state("api"), self.b.step_state("build")), ("running", "blocked"))
        self.assertIn("was withdrawn by the tech lead", self.member_hears(self.a, "a1", "tank"))
        self.assertNotIn("withdrawn", self.member_hears(self.b, "b1", "link"))

    def test_a_moot_decision_closes_with_its_run_and_the_other_runs_decision_still_waits(self):
        self.a.task_stop("a1")  # its asking member ended: nobody waits for D1 of run S1
        self.assertNotIn("Keep the old error format?", self.b.ok("status"))
        self.assertIn("Tag on Friday?", self.a.ok("status"))  # run S2's D1 still needs the developer
        self.a.ok("progress", "done", "Invoice export paused", "--force", "--why", "the developer stopped the work")
        self.assertEqual(self.a.decision("D1")["state"], "closed")
        self.assertEqual(self.b.decision("D1")["state"], "pending")
        self.assertIn("Tag on Friday?", self.b.ok("crew", "decisions", "--pending"))


# --------------------------------------------------------------------------- runs and lanes

class RunsAndLanes(Crew):

    def test_a_members_step_closes_only_in_its_own_run(self):
        a = self.claude(S1, self.api)
        b = self.claude(S2, self.api)
        k = self.claude(S3, self.kit)
        for s, aid in ((a, "a1"), (b, "b1")):
            self.staff(s, "Invoice export", "build=30m [backend]; review=20m after build [review]",
                       (aid, "tank", "Tank", "build", "backend"))
        k.ok("progress", "start", "Proposal pages", "--plan", "build=30m; review=20m after build")
        a.ok("progress", "step", "build done", "--step", "build", "--check", "12 tests pass", member="a1",
             member_type="devkit:tank")
        a.task_stop("a1")  # its lane ends with its evidence recorded
        lane = a.run_doc()["agents"]["a1"]
        self.assertEqual([u["text"] for u in lane["updates"]], ["build done"])
        self.assertEqual((b.step_state("build"), k.step_state("build")), ("running", "running"))
        self.assertNotIn("build done", json.dumps(b.run_doc()) + json.dumps(k.run_doc()))
        self.assertEqual(b.run_doc()["agents"]["b1"].get("updates") or [], [])

    def test_a_members_progress_call_goes_to_its_lane_through_the_ledger_when_the_rewrite_is_dropped(self):
        a = self.claude(S1, self.api)
        self.staff(a, "Invoice export", "build=30m [backend]; review=20m after build [review]",
                   ("a1", "tank", "Tank", "build", "backend"))
        r = a.sh("dev progress step 'review looks fine' --step review --check 'read it'", member="a1",
                 member_type="devkit:tank", raw=True)
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(a.step_state("review"), "pending")  # the main plan's step never moved
        self.assertEqual([u["text"] for u in a.run_doc()["agents"]["a1"]["updates"]], ["review looks fine"])

    def test_two_sessions_of_one_repository_with_the_same_command_open_each_write_their_own_run(self):
        a = self.claude(S1, self.api)
        b = self.claude(S2, self.api)
        a.ok("progress", "start", "Invoice export", "--plan", "api=30m")
        b.ok("progress", "start", "Release 1.2", "--plan", "build=30m")
        calls = [a.begin("dev progress note 'checking the build'"), b.begin("dev progress note 'checking the build'")]
        for s, c in zip((a, b), calls):  # both calls open at once, as in two terminals
            s.execute(c)
        for s, c in zip((a, b), calls):
            s.finish(c)
            self.assertEqual(c["rc"], 0, c)
        for s in (a, b):
            self.assertEqual([u["text"] for u in s.run_doc()["updates"] if u.get("kind") == "note"],
                             ["checking the build"])

    def test_a_compacted_conversation_keeps_its_run_and_the_other_session_keeps_its_own(self):
        a = self.claude(S1, self.api)
        b = self.claude(S2, self.api)
        self.staff(a, "Invoice export", "api=30m [backend]", ("a1", "tank", "Tank", "api", "backend"))
        b.ok("progress", "start", "Release 1.2", "--plan", "build=30m")
        a.ok("crew", "ask", "May I rename the invoice table?", member="a1", member_type="devkit:tank")
        before = b.run_doc()
        # Claude Code gives the conversation a new session id after a compaction, in the same process
        a2 = Session(self, a.host, "claude", "5e1f0a2b-0000-4000-8000-00000000c0de", self.api)
        a2.exports = dict(a.exports)  # its shells still name the older id until the next start
        a2.hook("session-start", a2.payload("SessionStart", source="compact"))
        self.assertEqual((a.has_run(), a2.has_run()), (False, True))
        self.assertEqual(a2.run_doc()["goal"], "Invoice export")
        self.assertEqual(b.run_doc(), before)
        self.assertIn("May I rename the invoice table?", a2.ok("crew", "inbox"))
        self.assertIn("no new messages", b.ok("crew", "inbox"))

    def test_carry_takes_no_run_from_a_live_session_of_another_project(self):
        a = self.claude(S1, self.api)
        a.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]; ui=30m [frontend]")
        k = self.claude(S3, self.kit)
        r = k.dev("session", "carry", "--from", S1)
        self.assertEqual(r["rc"], 2, r)
        self.assertIn("dev session tell @5e1f0a2b", r["err"])  # what to do instead: ask the run's owner
        self.assertEqual((a.has_run(), k.has_run()), (True, False))
        self.assertEqual(a.run_doc()["project"], "acme/api")

    def test_status_next_graph_and_dashboard_show_each_run_under_its_own_session_and_project(self):
        a = self.claude(S1, self.api)
        b = self.claude(S2, self.api)
        k = self.claude(S3, self.kit)
        self.staff(a, "Invoice export", "api=30m [backend]", ("a1", "tank", "Tank", "api", "backend"))
        self.staff(b, "Release 1.2", "build=30m [release]", ("b1", "link", "Link", "build", "release"))
        self.staff(k, "Proposal pages", "pages=1h [frontend]", ("k1", "trinity", "Trinity", "pages", "frontend"))
        a.ok("crew", "ask", "Old error format?", member="a1", member_type="devkit:tank")
        b.ok("crew", "ask", "Push the release branch?", member="b1", member_type="devkit:link")
        nxt = a.ok("crew", "next")
        self.assertIn("Invoice export", nxt)
        self.assertNotIn("Release 1.2", nxt)
        self.assertNotIn("Proposal pages", nxt)
        graph = json.loads(a.ok("crew", "graph", "--json"))
        self.assertEqual(graph["project"], "acme/api")
        self.assertEqual(sorted((g["session"]["session"], g["session"]["project"]) for g in graph["sessions"]),
                         [(S1, "acme/api"), (S2, "acme/api")])
        every = json.loads(k.ok("crew", "graph", "--global", "--json"))
        self.assertEqual(sorted((g["session"]["session"], g["session"]["project"]) for g in every["sessions"]),
                         sorted([(S1, "acme/api"), (S2, "acme/api"), (S3, "firefly/kit-consulting")]))
        theirs = k.ok("status")
        self.assertNotIn("Invoice export", theirs)
        self.assertNotIn("Old error format", theirs)
        page = self.home / "dashboard.html"
        k.ok("dashboard", "--export", str(page))
        html = page.read_text()
        for ref in ("M1@5e1f0a2b", "M1@c0ffee00"):  # the page has no session: each question names its own run
            self.assertIn(ref, html)


# --------------------------------------------------------------------------- crew slots

class SlotsAcrossSessions(Crew):
    """progress.max_parallel and progress.reserve hold per session: how the developer spends their quota across
    sessions is theirs to decide. Only the usage lowering is the account's, so it applies to every session."""

    def two(self):
        a = self.claude(S1, self.api)
        b = self.claude(S2, self.api)
        a.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]; ui=30m [frontend]")
        b.ok("progress", "start", "Statements", "--plan", "db=30m [database]; docs=30m [docs]")
        return a, b

    def working(self) -> int:
        n = 0
        for f in (self.state / "runs").glob("*.json"):
            n += sum(1 for v in (json.loads(f.read_text()).get("agents") or {}).values() if v.get("state") == "running")
        return n

    def test_each_session_has_its_own_crew_slots(self):
        self.config(max_parallel=2, reserve=0)
        a, b = self.two()
        self.assertEqual(a.launch("a1", "tank", "Tank: api"), "")
        self.assertEqual(a.launch("a2", "trinity", "Trinity: ui"), "")
        self.assertIn("2 of 2 crew slots are in use", a.launch("a3", "neo", "Neo: extra"))  # its own limit holds
        self.assertEqual(b.launch("b1", "dozer", "Dozer: db"), "")  # session S1's full slots never block S2's
        self.assertEqual(b.launch("b2", "ghost", "Ghost: docs"), "")
        self.assertEqual(self.working(), 4)
        self.assertIn("2 of 2 crew slots are in use", b.launch("b3", "neo", "Neo: extra"))
        a.task_stop("a1")  # a slot frees in S1 only: S2 is still at its limit
        self.assertIn("2 of 2 crew slots are in use", b.launch("b3", "neo", "Neo: extra"))
        self.assertEqual(a.launch("a3", "neo", "Neo: extra"), "")

    def test_the_usage_lowering_applies_to_every_session(self):
        self.config(max_parallel=4, reserve=0)
        quota = self.state / "quota" / "claude.json"
        quota.parent.mkdir(parents=True, exist_ok=True)
        quota.write_text(json.dumps({"five_hour": {"used_percentage": 87, "resets_at": time.time() + 3600}}))
        a, b = self.two()
        for s in (a, b):
            self.assertIn("lowered from 4: 5-hour usage at 87 %", s.ok("crew", "next"))
        self.assertEqual(b.launch("b1", "dozer", "Dozer: db"), "")
        self.assertEqual(b.launch("b2", "ghost", "Ghost: docs"), "")
        self.assertIn("the limit is 2 at once", b.launch("b3", "neo", "Neo: extra"))

    def test_launching_out_of_order_in_one_session_leaves_the_other_sessions_order_alone(self):
        a, b = self.two()
        for s, steps in ((a, (("api", "Tank", "backend"), ("ui", "Trinity", "frontend"))),
                         (b, (("db", "Dozer", "database"), ("docs", "Ghost", "docs")))):
            for step, name, activity in steps:
                s.ok("progress", "assign", step, "--to", name, "--scope", f"{step}/**", "--activity", activity)
        before = b.ok("crew", "next")
        self.assertIn("Ready to launch, in this order", before)
        a.ok("crew", "next", "--first", "ui", "--why", "the developer wants the screen first")
        self.assertEqual(sorted(a.run_doc().get("order_why") or {}), ["ui"])
        self.assertEqual(b.run_doc().get("order_why") or {}, {})
        self.assertEqual(b.ok("crew", "next"), before)


# --------------------------------------------------------------------------- Codex threads

class CodexThreads(Crew):
    """Two threads of one Codex app (one process, UUIDv7 thread ids that share their first 8 characters) and a Claude
    Code conversation, all in acme/api."""

    def setUp(self):
        super().setUp()
        self.x = self.codex(CX1, self.api)
        self.y = self.codex(CX2, self.api)
        self.c = self.claude(S1, self.api)
        for s, goal, aid, member in ((self.x, "Invoice export", "x1", "tank"), (self.y, "Statements", "y1", "dozer"),
                                     (self.c, "Release 1.2", "c1", "link")):
            s.ok("progress", "start", goal, "--plan", "work=30m")
            s.launch(aid, member, f"{member.title()}: work")
            s.ok("crew", "ask", f"question of {goal}", member=aid, member_type=member if s.agent == "codex"
                 else f"devkit:{member}")

    def test_each_thread_and_the_claude_session_keep_their_own_questions(self):
        for s, goal in ((self.x, "Invoice export"), (self.y, "Statements"), (self.c, "Release 1.2")):
            self.assertEqual([m["text"] for m in s.messages()], [f"question of {goal}"])
            inbox = s.ok("crew", "inbox")
            self.assertIn(f"question of {goal}", inbox)
            for other in {"Invoice export", "Statements", "Release 1.2"} - {goal}:
                self.assertNotIn(f"question of {other}", inbox)

    def test_threads_whose_ids_share_8_characters_get_distinct_qualified_ids_that_answer_the_right_one(self):
        status = self.c.ok("status")
        ids = sorted(w for w in status.replace('"', " ").split() if w.startswith("M1@01a1c178"))
        self.assertEqual(len(set(ids)), 2, status)
        self.assertTrue(all(len(i.split("@")[1]) > 8 for i in ids), ids)
        target = next(i for i in ids if CX2.startswith(i.split("@")[1]))
        self.c.ok("crew", "answer", target, "use the statement date")
        self.assert_waiting(self.y)
        self.assert_waiting(self.x, "M1")
        self.assertEqual([m["text"] for m in self.y.messages() if m.get("kind") == "answer"], ["use the statement date"])

    def test_two_threads_with_the_same_command_open_each_read_their_own_inbox(self):
        calls = [self.x.begin("dev crew inbox"), self.y.begin("dev crew inbox")]
        for s, c in zip((self.x, self.y), calls):  # both calls open at once, as two threads working in parallel
            s.execute(c)
        for s, c in zip((self.x, self.y), calls):
            s.finish(c)
        self.assertIn("question of Invoice export", calls[0]["out"])
        self.assertIn("question of Statements", calls[1]["out"])

    def test_a_codex_members_progress_goes_to_its_lane_in_its_own_thread(self):
        self.x.ok("progress", "step", "parser done", "--check", "8 tests pass", member="x1", member_type="tank")
        self.assertEqual([u["text"] for u in self.x.run_doc()["agents"]["x1"]["updates"]], ["parser done"])
        self.assertEqual(self.x.step_state("work"), "running")  # the main plan's step never moved
        for s in (self.y, self.c):
            self.assertNotIn("parser done", json.dumps(s.run_doc()))


if __name__ == "__main__":
    unittest.main()
