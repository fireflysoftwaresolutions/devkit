# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-based hunt for crew coordination faults. Seeded random sequences of what the tech lead and up to three crew
members do in one run (assign, launch, a member's step record, block, skip, reopen, resume, a question and its answer,
a wait, a stop through the SubagentStop hook and its crew gates) drive the real progress records, on Claude Code or
on Codex (each sequence's launches pick the host of its session). After every action the run is checked against the
rules the crew relies on; each rule is one test below, and a broken rule fails with the shortest sequence that still
breaks it (delta debugging over the first counterexample found). Standard library only, seeded, deterministic.
Run: python3 -m unittest tests.test_coordination_model"""
import contextlib
import copy
import io
import json
import os
import random
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import canary, core, hooks, lessonevents  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

REAL_CALLER = pg.caller  # the call ledger's own verdict; TempState replaces pg.caller for the actions themselves
SEED, SEQUENCES, LENGTH = 20261119, 48, 28
MEMBERS = {"tank": ("Tank", "Backend engineer"), "trinity": ("Trinity", "Frontend engineer"),
           "seraph": ("Seraph", "Code reviewer")}
PLAN = "api=30m [backend]; export=30m [backend]; ui=30m [frontend]; polish=20m [frontend]; review=20m [review] after api"
STEPS = {"api": "tank", "export": "tank", "ui": "trinity", "polish": "trinity", "review": "seraph"}
SCOPES = {"api": "api/**", "export": "export/**", "ui": "ui/**", "polish": "polish/**", "review": ""}
OPEN = ("running", "pending", "blocked")


KINDS = ["assign", "launch", "step", "block", "skip", "reopen", "resume", "ask", "answer", "wait", "stop"]


def propose(rng: random.Random, w=None) -> dict:
    """One abstract action. With the world at hand, most proposals follow a step's life (assign it, launch its
    member, record it, stop, reopen, resume), so a sequence reaches deep states; the rest are uniform."""
    kind, member, step = rng.choice(KINDS), rng.choice(sorted(MEMBERS)), rng.choice(sorted(STEPS))
    if w is not None and rng.random() < 0.7:
        step = rng.choice(sorted(STEPS) + ["review", "review"])  # the review's life is the longest chain
        plan = w.plan()
        step = next((d for d in plan[step].get("after") or [] if plan[d]["state"] not in ("done", "skipped")), step)
        member, s = STEPS[step], plan[step]
        lane = w.lanes.get(member)
        if s["state"] in ("done", "skipped", "unverified"):
            again = 0.8 if s["state"] == "unverified" else 0.3  # unverified work waits for a reopen
            kind = "reopen" if rng.random() < again else ("resume" if lane in w.ended else "stop")
        elif not s.get("label"):
            kind = "assign"
        elif not w.live(member):
            kind = "resume" if lane in w.ended and rng.random() < 0.8 else "launch"
        elif not any(f"--step {step}" in c for c in w.commands.get(w.live(member), [])):
            kind = rng.choice(["step", "step", "step", "ask", "wait", "answer", "assign"])  # record it first
        else:
            kind = rng.choice(["stop", "stop", "step", "ask", "wait", "answer"])
    if kind in ("assign", "block", "skip", "reopen"):
        member = STEPS[step]
    return {"do": kind, "member": member, "step": step, "how": rng.choice(["role", "label"]),
            "named": rng.random() < 0.6, "clean": rng.random() < 0.6}


def generate(rng: random.Random, host: str) -> tuple:
    """One sequence, built by running it: each slot takes the first of a few proposals that applies -> (the
    actions, the rules they broke)."""
    seq = []
    with World(host) as w:
        for _ in range(LENGTH):
            for _ in range(6):
                a = propose(rng, w)
                if w.act(a):
                    seq.append(a)
                    break
        return seq, w.violations


def name_of(a):
    return MEMBERS[a["member"]][0]


def show(seq: list) -> str:
    def one(a):
        extra = {"assign": f"{a['step']} by {a['how']}", "step": f"--step {a.get('used')}" if a["named"] else "(no --step)",
                 "stop": "clean" if a["clean"] else "sent back, then let through", "block": a["step"],
                 "skip": a["step"], "reopen": a["step"], "launch": f"“{name_of(a)}: {a.get('used')}”", "resume": "", "ask": "", "answer": "",
                 "wait": ""}[a["do"]]
        who = "" if a["do"] in ("assign", "block", "skip", "reopen", "answer") else MEMBERS[a["member"]][0] + " "
        return f"{a['do']} {who}{extra}".strip()
    return " → ".join(f"{i}. {one(a)}" for i, a in enumerate(seq, 1))


class World:
    """One run in a temporary devkit home, driven through the real progress API and hooks."""

    def __init__(self, host: str):
        self.host = host
        self.violations = {}
        self.lanes, self.ended, self.count, self.calls, self.commands = {}, set(), 0, {}, {}
        self.waiting_q = []  # (message id, member) of questions waiting for the tech lead's answer
        self.t0 = time.time() - 50 * 60
        self.k = 0

    def __enter__(self):
        self.state = TempState().__enter__()
        root = Path(self.state.tmp.name)
        tools = root / "bin"
        tools.mkdir()
        (tools / "dev").symlink_to(core.DEVKIT_HOME / "bin" / "dev")
        self.patches = [mock.patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"],
                                                     "DEVKIT_SESSION": f"{self.host}:s1"}),
                        mock.patch.object(hooks, "STATE", root), mock.patch.object(canary, "LOG", root / "canary"),
                        mock.patch.object(lessonevents, "EVENTS", root / "lesson-events")]
        for p in self.patches:
            p.start()
        if self.host == "codex":
            (pg.SESSIONS / "codex-s1.json").write_text(json.dumps({"agent": "codex", "session": "s1",
                                                                  "started": self.t0, "project": "acme/api"}))
        pg.start("Invoice export", plan=PLAN, now=self.now())
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        self.state.__exit__(*exc)

    def now(self):
        self.k += 1
        return self.t0 + self.k * 60

    def run_(self):
        return pg.load_run(self.host, "s1")

    def plan(self):
        return {s["name"]: s for s in self.run_()["plan"]}

    def live(self, member):
        aid = self.lanes.get(member)
        return aid if aid and (self.run_()["agents"].get(aid) or {}).get("state") == "running" else None

    # ---------------------------------------------------------------- the call ledger

    def open_call(self, cmd, aid=None):
        self.count += 1
        data = {"session_id": "s1", "tool_use_id": f"toolu_m{self.count:04d}", "tool_name": "Bash",
                "tool_input": {"command": cmd}}
        if aid:
            data["agent_id"] = aid
        pg.call_opened(self.host, data)
        return data

    def close_member_call(self, member):
        data = self.calls.pop(member, None)
        if data:
            pg.call_closed(data)

    def main(self, argv, fn):
        """A command of the tech lead's: its open call in the ledger, attributed as the ledger reads it (I5)."""
        data = self.open_call(" ".join(["dev"] + [json.dumps(a) if " " in a else a for a in argv]))
        try:
            who = REAL_CALLER(argv=argv)["caller"]
            if who not in ("main", "unverified"):
                self.flag("main-calls-are-mains", f"the tech lead's `dev {' '.join(argv[:3])}` was attributed to "
                          f"{who} while members waited in: " + "; ".join(
                              f"{MEMBERS[m][0]}: {c['tool_input']['command'][:70]}" for m, c in self.calls.items()))
            with mock.patch.object(sys, "argv", ["dev", *argv]):
                return fn()
        except pg.StepError:
            return None
        finally:
            pg.call_closed(data)

    def flag(self, rule, text):
        self.violations.setdefault(rule, text)

    # ---------------------------------------------------------------- actions

    def act(self, a):
        member, step = a["member"], a["step"]
        name = MEMBERS[member][0]
        before = {k: v.get("state") for k, v in self.plan().items()}
        if a["do"] not in ("ask", "wait"):
            self.close_member_call(member)  # a member that acts again has finished its wait
        expect = None
        if a["do"] == "assign":
            aid = self.lanes.get(member)
            label = name if a["how"] == "role" or not aid else self.run_()["agents"][aid]["label"]
            argv = ["progress", "assign", step, "--to", label] + (["--scope", SCOPES[step]] if SCOPES[step] else [])
            self.main(argv, lambda: pg.assign(step, label, scope=SCOPES[step], why="model", now=self.now()))
        elif a["do"] == "launch":
            if self.live(member) or (self.lanes.get(member) and self.lanes[member] not in self.ended):
                return False
            mine = [s for s, m in STEPS.items() if m == member and self.plan()[s]["state"] == "pending"
                    and (self.plan()[s].get("label") or "").lower().startswith(name.lower())]
            task = mine[0] if mine else step if STEPS[step] == member else "work"
            a["used"] = task
            aid = f"a{member}{len(self.lanes) + len(self.ended) + 1}"
            try:
                launch(aid, member if self.host == "codex" else f"devkit:{member}", f"{name}: {task}", at=self.now(),
                       agent=self.host)
            except AssertionError:  # Codex's admission refused the launch (a slot, the order): nothing started
                return False
            self.lanes[member] = aid
            self.ended.discard(aid)
        elif a["do"] == "step":
            aid = self.live(member)
            if not aid:
                return False
            running = [s for s, st in self.plan().items() if st.get("owner") == aid and st.get("state") == "running"]
            which = (running[0] if running else step) if a["named"] else None
            a["used"] = which
            text = f"{which or 'my work'} committed"
            try:
                pg.step(text, which=which, check="✓ 12 tests pass", from_agent=aid, now=self.now(),
                        approves="api" if which == "review" else "")
            except pg.StepError:
                return False
            self.commands.setdefault(aid, []).append(
                f'dev progress step "{text}"' + (f' --step {which}' if which else "") + ' --check "✓ 12 tests pass"')
            # I3b: the record closes the step it names, or its lane's only running step when it names none
            target = which if which in running else (running[0] if not which and len(running) == 1 else None)
            if target and target != "review":
                expect = target
        elif a["do"] == "block":
            if before[step] not in ("running", "pending"):
                return False
            self.main(["progress", "block", "waits for the developer", "--step", step],
                      lambda: pg.step("waits for the developer", kind="blocked", which=step, now=self.now()))
        elif a["do"] == "skip":
            if before[step] in ("done", "skipped"):
                return False
            self.main(["progress", "skip", step, "--why", "out of scope"],
                      lambda: pg.reopen(step, "out of scope", skip=True, now=self.now()))
        elif a["do"] == "reopen":
            if before[step] not in ("done", "skipped", "unverified"):
                return False
            self.main(["progress", "reopen", step, "--why", "the review found a gap"],
                      lambda: pg.reopen(step, "the review found a gap", now=self.now()))
        elif a["do"] == "resume":
            aid = self.lanes.get(member)
            if not aid or aid not in self.ended:
                return False
            pg.on_subagent(self.host, {"session_id": "s1", "agent_id": aid,
                                       "agent_type": member if self.host == "codex" else f"devkit:{member}"}, True,
                           now=self.now())
            self.ended.discard(aid)
        elif a["do"] in ("ask", "wait"):
            aid = self.live(member)
            if not aid or member in self.calls:
                return False
            if a["do"] == "ask":
                cmd = f'dev crew ask "May I go on with {step}? (dev progress begin {step} or whatever links it)"'
                try:
                    q = pg.crew_ask(cmd[len('dev crew ask "'):-1], from_agent=aid, now=self.now())
                    self.waiting_q.append((q["id"], member))
                except Exception:
                    return False
            else:
                cmd = "dev watch --wait --for me --timeout 9"
            self.calls[member] = self.open_call(cmd, aid)  # in the foreground: open until the member acts again
            if a["do"] == "ask":  # the tech lead's begin of the step the question quotes, while it waits
                self.main(["progress", "begin", step], lambda: pg.begin(step, now=self.now()))
        elif a["do"] == "answer":
            if not self.waiting_q:
                return False
            mid, asker = self.waiting_q.pop(0)
            self.main(["crew", "answer", mid, "yes, go on"], lambda: pg.crew_answer(mid, "yes, go on", now=self.now()))
            self.close_member_call(asker)
        elif a["do"] == "stop":
            aid = self.live(member)
            if not aid:
                return False
            review = self.plan()["review"]
            ready = review.get("owner") == aid and review.get("state") == "running" and any(
                "--step review" in c for c in self.commands.get(aid, []))
            if a["clean"]:
                if not self.stop(aid, member, self.commands.get(aid, [])):
                    ready = False  # the gates sent it back for something else: the lane works on
                    self.ended.discard(aid)
                    self.check(a, before, expect)
                    return True
            else:
                self.stop(aid, member, [])  # a transcript with no step record: sent back once
                self.stop(aid, member, [], again=True)
            self.ended.add(aid)
            if a["clean"] and ready:  # I4: a clean report of a ✓ record of its own review is judged on its own
                state = self.plan()["review"]["state"]
                if state != "done":
                    why = [h["why"] for h in self.run_().get("history") or [] if h["step"] == "review"][-1:]
                    self.flag("clean-review-approves", f"review stayed {state} after its reviewer's clean report: "
                              f"{why[0] if why else ''}")
        self.check(a, before, expect)
        return True

    def stop(self, aid, member, commands, again=False):
        """The member's stop through the SubagentStop hook: canary, crew gates, the lane's end."""
        root = Path(self.state.tmp.name)
        tr = root / f"transcript-{aid}-{self.k}.jsonl"
        if self.host == "codex":
            tr = self.codex_rollout(aid, commands, tr)
        records = [] if self.host == "codex" else None
        for i, command in enumerate(commands if records is not None else [], 1):
            records += [{"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [
                            {"type": "tool_use", "id": f"toolu_{i:04}", "name": "Bash", "input": {"command": command}}]}},
                        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": f"toolu_{i:04}", "content": "ok"}]}}]
        if records is not None:
            tr.write_text("".join(json.dumps(r) + "\n" for r in records))
        name, role = MEMBERS[member]
        report = (f"**[Firefly> DevKit · {role} ({name})]** api · `main` · the work\n- **Done:** the work\n"
                  "- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead reviews it")
        data = {"session_id": "s1", "agent_id": aid, "agent_type": member if self.host == "codex" else
                f"devkit:{member}", "agent_transcript_path": str(tr), "last_assistant_message": report,
                "stop_hook_active": again}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_stop(self.host)
        return '"block"' not in out.getvalue()  # the lane ended: the gates let this stop through

    def codex_rollout(self, aid, commands, tr):
        """A Codex member's own rollout as Codex 0.167.0 writes it: its session_meta (where its own history starts),
        task_started, then one CommandExecution item per command with its folder, every record with its ordinal."""
        child = next(pg.SESSIONS.parent.glob(f".codex/sessions/*/*/*/rollout-*-{aid}.jsonl"))
        meta = json.loads(child.read_text().splitlines()[0])
        meta["ordinal"] = 0
        meta["payload"]["subagent_history_start_ordinal"] = 1
        lines = [meta, {"timestamp": meta.get("timestamp"), "ordinal": 1, "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "t1"}}]
        for i, command in enumerate(commands, 2):
            lines.append({"timestamp": meta.get("timestamp"), "ordinal": i, "type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "t1", "item": {
                    "type": "CommandExecution", "id": f"exec-{aid}-{i}", "command": ["/bin/zsh", "-lc", command],
                    "cwd": f"file://{self.state.tmp.name}", "status": "completed", "exit_code": 0,
                    "aggregated_output": "✓"}}})
        tr.write_text("".join(json.dumps(r) + "\n" for r in lines))
        return tr

    # ---------------------------------------------------------------- the rules

    def check(self, a, before, expect):
        run = self.run_()
        plan, agents = {s["name"]: s for s in run["plan"]}, run.get("agents") or {}
        live = {aid for aid, lane in agents.items() if lane.get("state") == "running"}
        for name, s in plan.items():
            if s.get("state") == "running" and s.get("label") and s.get("owner", "main") != "main" \
                    and s["owner"] not in live:
                self.flag("running-step-has-a-live-lane", f"“{name}” runs in lane {s['owner']}, which is "
                          f"{(agents.get(s['owner']) or {}).get('state')}")
        for aid in live:
            layers = pg.lane_scope(run, aid)
            writable = {g for layer in layers for g in layer}
            mine = [s for s in plan.values() if s.get("owner") == aid]
            running = {g for s in mine if s.get("state") == "running" for g in s.get("scope") or []}
            for s in mine:
                files = set(s.get("scope") or []) - running
                if s.get("state") == "running" and not all(pg.in_scope(g.rstrip("*") + "x.py", layers)
                                                           for g in s.get("scope") or []):
                    self.flag("running-step-files-writable", f"lane {aid} runs “{s['name']}” but may not write "
                              f"its files {s.get('scope')}: its scope is {sorted(writable)}")
                if s.get("state") == "blocked" and files & writable:
                    self.flag("blocked-step-files-released", f"lane {aid} still holds blocked “{s['name']}”'s "
                              f"files {sorted(files & writable)}")
                if s.get("state") in ("done", "skipped") and files & writable:
                    self.flag("closed-step-files-released", f"lane {aid} still holds {s['state']} “{s['name']}”'s "
                              f"files {sorted(files & writable)}")
            if not [s for s in mine if s.get("state") == "running"]:
                member = (agents[aid].get("persona") or "").lower()
                ready = [s["name"] for s in plan.values() if s.get("state") == "pending" and
                         (s.get("owner") or "main") == "main" and STEPS.get(s["name"]) == member and
                         (s.get("label") or "").lower().startswith(member) and not pg.deps_open(run, s)
                         and not [x for x in pg._queued_lanes(s) if x != aid]]
                if ready:
                    self.flag("free-lane-takes-its-members-step", f"lane {aid} ({member}) holds no running step "
                              f"while {ready} wait for {member}")
        for name, s in plan.items():
            if s.get("state") == "done" and before.get(name) != "done":
                ok = (a["do"] == "step" and name == expect) or a["do"] == "stop" or \
                     (a["do"] == "step" and a["named"] and s.get("owner") == self.lanes.get(a["member"]))
                if not ok:
                    self.flag("closes-only-what-it-names", f"“{name}” closed on {a['do']} of {a['member']}")
        if expect and plan[expect].get("state") != "done":
            self.flag("checked-record-closes-its-step", f"{MEMBERS[a['member']][0]}'s ✓ record "
                      f"{'naming' if a['named'] else 'without --step, its lane running only'} “{expect}” left it "
                      f"{plan[expect].get('state')}")


RULES = {
    "running-step-has-a-live-lane": "a running step belongs to exactly one live lane",
    "running-step-files-writable": "a lane may write the files of each step it runs",
    "blocked-step-files-released": "a lane's write scope never keeps a blocked step's files",
    "closed-step-files-released": "a lane's write scope never keeps a done or skipped step's files",
    "free-lane-takes-its-members-step": "a live lane with no running step takes a ready step assigned to its member",
    "closes-only-what-it-names": "a step closes only by a record that names it, its lane's single running step, or "
                                 "its lane's end",
    "checked-record-closes-its-step": "a member's ✓ record closes the step it names, or its lane's only running step",
    "clean-review-approves": "a reviewer's ✓ record of its own review is judged by the current rules whatever "
                             "earlier send-backs",
    "main-calls-are-mains": "every main-session call is attributed to the main session whatever members wait",
}


def violations(host, seq) -> dict:
    with World(host) as w:
        for a in seq:
            w.act(a)
        return w.violations


def shrink(host, seq, rule) -> tuple:
    """The shortest subsequence that still breaks `rule` (one-at-a-time and halving removals), and its message."""
    best = copy.deepcopy(seq)  # its own actions: each records what it did in the replay
    text = violations(host, best)[rule]
    chunk = max(1, len(best) // 2)
    while chunk >= 1:
        i, changed = 0, False
        while i < len(best):
            trial = best[:i] + best[i + chunk:]
            got = violations(host, trial) if trial else {}
            if rule in got:
                best, text, changed = trial, got[rule], True
            else:
                i += chunk
        if not changed:
            chunk //= 2
    violations(host, best)  # replayed last, so each action shows what it did in this sequence
    return best, text


class CoordinationModelTests(unittest.TestCase):
    # Three rules the hunt also checks hold on 26.11.26 (a running step has a live lane, a lane may write its running
    # steps' files, a step closes only by what names it): they get tests of their own once a fix could break them
    found = None

    @classmethod
    def hunt(cls):
        if cls.found is None:
            rng, first = random.Random(SEED), {}
            for n in range(SEQUENCES):
                host = "codex" if n % 2 else "claude"
                seq, broken = generate(rng, host)
                for rule in broken:
                    first.setdefault(rule, (host, seq))
            cls.found = {rule: (host,) + shrink(host, seq, rule) for rule, (host, seq) in sorted(first.items())}
        return cls.found

    def holds(self, rule):
        got = self.hunt().get(rule)
        if got:
            host, seq, text = got
            self.fail(f"{RULES[rule]}: broken on {host} by {show(seq)}\n  → {text}")

    def test_a_lanes_scope_never_keeps_a_blocked_steps_files(self):
        self.holds("blocked-step-files-released")

    def test_a_lanes_scope_never_keeps_a_closed_steps_files(self):
        self.holds("closed-step-files-released")

    def test_a_free_lane_takes_a_ready_step_assigned_to_its_member(self):
        self.holds("free-lane-takes-its-members-step")

    def test_a_members_checked_record_closes_its_step(self):
        self.holds("checked-record-closes-its-step")

    def test_a_clean_review_report_approves_whatever_earlier_send_backs(self):
        self.holds("clean-review-approves")

    def test_every_main_session_call_is_the_main_sessions(self):
        self.holds("main-calls-are-mains")


if __name__ == "__main__":
    unittest.main()
