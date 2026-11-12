# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The lane router (guard.lane_route, hooks._route_subagent_progress) for a crew member's compound command: a lane call
# after `cd` and reads, or with a read-only filter piped after it, reaches the member's lane; message text in its
# quoted arguments is data. The commands are ones crew members were refused for in recorded sessions. Every allowed
# form has a twin that stays refused.
# Run: python3 -m unittest tests.test_guard_lane_router -v
import random
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard, hooks, progress  # noqa: E402

# the folder the commands run in: a project of the run's dev root (below its temporary home), so a lane's
# verdicts do not follow where this checkout lives (in a copy inside a session scratchpad the guard's
# scratch-folder rule would judge every path first)
CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")
WT = "/Users/x/Development/.worktrees/acme/devkit/feat-dev-ext"
HEAD = "DEVKIT_FROM_AGENT=a1 "

# recorded refusals of honest work: (the command, where the router puts the variable)
RECORDED = (
    (f'cd {WT} && bin/dev progress step "extension manager fixes two" --check "x; y && z | w (3 tests)"',
     f'cd {WT} && {HEAD}bin/dev progress step'),
    ('bin/dev crew inbox 2>&1 | tail -5', f'{HEAD}bin/dev crew inbox 2>&1 | tail -5'),
    ('dev progress note "sig rehearsal" --next "full suite" 2>&1 | tail -12', f'{HEAD}dev progress note'),
    ('git status --short && bin/dev crew inbox', f'git status --short && {HEAD}bin/dev crew inbox'),
    ('bin/dev crew inbox; git log --oneline -5', f'{HEAD}bin/dev crew inbox; git log'),
    ('bin/dev stubs 2>&1 | tail -3; bin/dev progress step "nested agents" --check "26e257d"',
     f'bin/dev stubs 2>&1 | tail -3; {HEAD}bin/dev progress step'),
    ('bin/dev progress 2>&1 | grep -n "review batch 5" | head', f'{HEAD}bin/dev progress 2>&1 | grep'),
    ('cd ~/Development/.worktrees/acme/devkit/fix-x && dev progress note "red: 12 new tests fail" --next "fix"',
     f'&& {HEAD}dev progress note'),
    ("cd lib && dev crew ask \"$(cat <<'EOF'\nuse x; then y && z | w\nEOF\n)\"", f'cd lib && {HEAD}dev crew ask'),
    ('bin/dev crew ask --help 2>&1 | head -30; bin/dev progress status 2>&1 | head -30',
     f'; {HEAD}bin/dev progress status'),
)

# what must stay refused: a write or a program next to the lane call, a substitution, a feeding pipe, a wrapper, a
# nested shell, a redirect to a file, control flow, a background job, two lane calls in one command
REFUSED = (
    "dev progress step x && rm -rf build", "cd lib && dev progress step x | tee /tmp/out",
    "echo y | dev crew decide D1 A", 'cd lib && dev progress step "$(id)"', 'cd lib && dev progress step "`id`"',
    "cd lib && dev progress step x; bash -c 'dev crew decide D1 A'", "cd lib && python3 bin/dev progress step x",
    "cd lib && env dev crew decide D1 A", "cd lib && dev progress step x > /tmp/f",
    "if true; then\ndev progress step x\nfi", "cd lib && dev progress step x & sleep 1",
    'cd lib && dev progress step x | python3 -c "import sys"', "git commit -m x && dev progress step x",
    'dev crew ask "x"; gh pr merge 12', "dev progress step x; dev crew ask y", "cd $(pwd) && dev progress step x",
    "for i in 1; do dev progress step x; done", "{ cd lib; dev progress step x; }", "(cd lib && dev progress step x)",
    "cd lib && D=dev; $D progress step x", "cd lib && dev progress step x # note\nrm -rf build",
    'cd lib && d""ev progress step x', "cd lib && dev progress step x | sort -o /tmp/out")


class TheRouterPlacesTheLaneCall(unittest.TestCase):

    def test_a_recorded_compound_lane_call_gets_the_lane_variable_in_front_of_it(self):
        for cmd, where in RECORDED:
            with self.subTest(cmd=cmd):
                new = guard.lane_route(cmd, "a1")
                self.assertIsNotNone(new)
                self.assertIn(where, new)
                self.assertEqual(new.replace(HEAD, "", 1), cmd)  # one insertion, the rest as typed
                self.assertEqual(new.count("DEVKIT_FROM_AGENT="), 1)

    def test_a_lane_call_next_to_a_write_a_substitution_or_a_second_call_is_not_routed(self):
        for cmd in REFUSED:
            with self.subTest(cmd=cmd):
                self.assertIsNone(guard.lane_route(cmd, "a1"))

    def test_the_lane_verdict_passes_the_routed_call_and_nothing_nested(self):
        for cmd, _ in RECORDED:
            for level in ("all", "review"):
                with self.subTest(cmd=cmd, level=level):
                    self.assertIsNone(guard.crew_shell_verdict(cmd, CWD, None, None, level=level, routed=True))
        for cmd in REFUSED:  # judged as routed or not, each stays refused
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(guard.crew_shell_verdict(cmd, CWD, None, None, level="all", routed=True))
                self.assertIsNotNone(guard.crew_shell_verdict(cmd, CWD, None, None, level="all"))

    def test_any_quoted_message_after_cd_is_data(self):
        # property: whatever the quoted message says, the router inserts the variable once, before the call
        rng = random.Random(20261108)
        parts = ["dev progress step x", "dev crew decide D1 A", "bash -c ", "$(", ")", "`", ";", "&&", "|", "\n",
                 "<<EOF", "EOF", '"', "'", "\\", "{ ", " }", "then ", "python3 -c ", " ", "#", "> /tmp/f", "&"]
        for _ in range(400):
            text = "".join(rng.choice(parts) for _ in range(rng.randint(1, 12)))
            for verb in ("crew ask", "crew answer M3", "progress step"):
                cmd = f"cd lib && dev {verb} {shlex.quote(text)} 2>&1 | tail -3"
                with self.subTest(cmd=cmd):
                    self.assertEqual(guard.lane_route(cmd, "a1"),
                                     f"cd lib && {HEAD}dev {verb} {shlex.quote(text)} 2>&1 | tail -3")

    def test_an_agent_id_the_shell_would_read_is_never_written_into_the_command(self):
        for aid in ("a1;rm", "a b", "$(id)", "", "a" * 81):
            with self.subTest(aid=aid):
                self.assertIsNone(guard.lane_route("cd lib && dev progress step x", aid))


class TheRefusalSaysWhatKeptTheCallBack(unittest.TestCase):
    # three texts sent members the wrong way: a lane call alone, with backticks in its double-quoted message, was told
    # to run as a command of its own; an unknown `dev crew` word was told it changes the team; and a program named
    # nothing the guard found in it

    def verdict(self, cmd):
        v = guard.crew_shell_verdict(cmd, CWD, None, "neo", level="all", lane="a1", session="s1", agent="claude",
                                     routed=guard.lane_route(cmd, "a1") is not None, grant=lambda *a, **k: None)
        return v[1] if v else None

    def test_a_substitution_in_a_double_quoted_message_is_named(self):
        for cmd, what in (('dev crew ask "Should I run `dev push` now?"', "backticks"),
                          ('dev crew answer M166 "Yes: use `dev test` first."', "backticks"),
                          ('dev progress note "the cause is in $(cat notes)" --next "fix"', "`$(`")):
            with self.subTest(cmd=cmd):
                why = self.verdict(cmd) or ""
                self.assertIn(f"holds {what}", why)
                self.assertIn("single quotes", why)
        self.assertEqual(self.verdict("dev crew inbox; ruff check lib"), guard.LANE_REASON)  # a chain: as before
        self.assertIsNone(self.verdict("dev crew ask 'Should I run `dev push` now?'"))

    def test_a_word_that_is_no_crew_command_is_named_as_such(self):
        why = self.verdict("dev crew messages") or ""
        self.assertIn("`dev crew messages` is not a crew command", why)
        self.assertIn("`inbox`", why)
        self.assertNotIn("changes the team", why)
        self.assertIn("changes the team's crew", self.verdict("dev crew brief tank --step api") or "")

    def test_a_program_names_the_construct_that_makes_it_unknown(self):
        why = self.verdict("python3 - <<'PY'\nimport argparse\np = argparse.ArgumentParser()\n"
                           "print(vars(p.parse_args([]))[input()])\nPY") or ""
        self.assertIn("`vars(p.parse_args([]))[input()]`", why)
        self.assertNotIn("whose name is only known", why)
        self.assertIn("whose name is only known", self.verdict("$X pr merge 12") or "")  # a name the shell builds


class TheHookRoutesItToTheLane(unittest.TestCase):
    """hooks._route_subagent_progress and _unroutable_lane_command on a member's call, its lane mocked as in
    test_guard_crew.HookWiringTests: a review member (the Code reviewer) and a write member (the DevEx engineer)."""

    def lane(self, persona, level):
        run = {"agents": {"a1": {"persona": persona}}}
        for name, value in (("lane_of", lambda *a: (run, run["agents"]["a1"])), ("lane_chain", lambda r, a: [a]),
                            ("lane_tools", lambda r, a: level), ("lane_scope", lambda r, a: None)):
            p = mock.patch.object(progress, name, value)
            p.start()
            self.addCleanup(p.stop)

    def data(self, cmd):
        return {"agent_id": "a1", "tool_name": "Bash", "session_id": "s", "cwd": CWD, "tool_input": {"command": cmd}}

    def test_a_recorded_compound_call_is_routed_in_a_review_and_a_write_lane(self):
        for persona, level in (("Seraph", "review"), ("Kid", "all")):
            self.lane(persona, level)
            for cmd, where in RECORDED:
                with self.subTest(cmd=cmd, persona=persona):
                    routed = hooks._route_subagent_progress(self.data(cmd))
                    self.assertIn(where, (routed or {}).get("command", ""))
                    self.assertIsNone(hooks._unroutable_lane_command(self.data(cmd)))

    def test_a_compound_call_with_a_write_stays_refused_with_the_lane_reason(self):
        self.lane("Kid", "all")
        for cmd in ("cd lib && dev progress step x && rm -rf build", "echo y | dev crew decide D1 A",
                    "cd lib && dev progress step x | tee /tmp/out", "cd lib && env dev crew decide D1 A"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(hooks._route_subagent_progress(self.data(cmd)))
                self.assertIn("command of its own", hooks._unroutable_lane_command(self.data(cmd)) or "")

    def test_a_routed_compound_call_still_meets_the_lane_rules(self):
        # a read the router accepts is judged again by the lane: here an irreversible push in a review lane
        self.lane("Seraph", "review")
        cmd = "cd lib && dev progress step x && git push origin main"
        self.assertIsNone(hooks._route_subagent_progress(self.data(cmd)))
        self.assertIsNotNone(hooks._unroutable_lane_command(self.data(cmd)))


if __name__ == "__main__":
    unittest.main()
