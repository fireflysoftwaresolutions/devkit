# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The lane router (lib/devkit/laneroute.py) for a crew member's shell call: a `dev progress …` or `dev crew …` call
# alone, after `cd` and reads, or with a read-only filter piped after it, gets DEVKIT_FROM_AGENT=<agent id> in front of
# it, so it acts as the member's lane; message text in its quoted arguments is data. The commands are ones crew members
# ran in recorded sessions. Every routed form has a twin the router leaves alone. No command runs.
# Run: python3 -m unittest tests.test_laneroute -v
import json
import random
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, laneroute  # noqa: E402

CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")
WT = "/Users/x/Development/.worktrees/acme/devkit/feat-dev-ext"
HEAD = "DEVKIT_FROM_AGENT=a1 "

# recorded calls of honest work: (the command, where the router puts the variable)
ROUTED = (
    ("dev progress step x", f"{HEAD}dev progress step x"),
    (f'cd {WT} && bin/dev progress step "extension manager fixes two" --check "x; y && z | w (3 tests)"',
     f'cd {WT} && {HEAD}bin/dev progress step'),
    ('bin/dev crew inbox 2>&1 | tail -5', f'{HEAD}bin/dev crew inbox 2>&1 | tail -5'),
    ('dev progress note "sig rehearsal" --next "full suite" 2>&1 | tail -12', f'{HEAD}dev progress note'),
    ('git status --short && bin/dev crew inbox', f'git status --short && {HEAD}bin/dev crew inbox'),
    ('bin/dev crew inbox; git log --oneline -5', f'{HEAD}bin/dev crew inbox; git log'),
    ('bin/dev stubs 2>&1 | tail -3; bin/dev progress step "nested agents" --check "26e257d"',
     f'bin/dev stubs 2>&1 | tail -3; {HEAD}bin/dev progress step'),
    ('bin/dev progress 2>&1 | grep -n "review batch 5" | head', f'{HEAD}bin/dev progress 2>&1 | grep'),
    ("cd lib && dev crew ask \"$(cat <<'EOF'\nuse x; then y && z | w\nEOF\n)\"", f'cd lib && {HEAD}dev crew ask'),
    ('bin/dev crew ask --help 2>&1 | head -30; bin/dev progress status 2>&1 | head -30',
     f'; {HEAD}bin/dev progress status'),
)

# what the router leaves alone: a write or a program next to the lane call, a substitution, a feeding pipe, a
# wrapper, a nested shell, a redirect to a file, control flow, a background job, two lane calls in one command, and a
# command with no lane call
NOT_ROUTED = (
    "dev progress step x && rm -rf build", "cd lib && dev progress step x | tee /tmp/out",
    "echo y | dev crew decide D1 A", 'cd lib && dev progress step "$(id)"', 'cd lib && dev progress step "`id`"',
    "cd lib && dev progress step x; bash -c 'dev crew decide D1 A'", "cd lib && python3 bin/dev progress step x",
    "cd lib && env dev crew decide D1 A", "cd lib && dev progress step x > /tmp/f",
    "if true; then\ndev progress step x\nfi", "cd lib && dev progress step x & sleep 1",
    'cd lib && dev progress step x | python3 -c "import sys"', "git commit -m x && dev progress step x",
    'dev crew ask "x"; gh pr merge 12', "dev progress step x; dev crew ask y", "cd $(pwd) && dev progress step x",
    "for i in 1; do dev progress step x; done", "{ cd lib; dev progress step x; }", "(cd lib && dev progress step x)",
    "cd lib && D=dev; $D progress step x", "cd lib && dev progress step x # note\nrm -rf build",
    'cd lib && d""ev progress step x', "cd lib && dev progress step x | sort -o /tmp/out", "git status",
    "dev crew brief tank --step api")


class RouteTests(unittest.TestCase):

    def test_a_recorded_lane_call_gets_the_lane_variable_in_front_of_it(self):
        for cmd, where in ROUTED:
            with self.subTest(cmd=cmd):
                new = laneroute.route(cmd, "a1")
                self.assertIsNotNone(new)
                self.assertIn(where, new)
                self.assertEqual(new.replace(HEAD, "", 1), cmd)  # one insertion, the rest as typed
                self.assertEqual(new.count("DEVKIT_FROM_AGENT="), 1)

    def test_a_lane_call_next_to_a_write_a_substitution_or_a_second_call_is_not_routed(self):
        for cmd in NOT_ROUTED:
            with self.subTest(cmd=cmd):
                self.assertIsNone(laneroute.route(cmd, "a1"))

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
                    self.assertEqual(laneroute.route(cmd, "a1"),
                                     f"cd lib && {HEAD}dev {verb} {shlex.quote(text)} 2>&1 | tail -3")

    def test_an_agent_id_the_shell_would_read_is_never_written_into_the_command(self):
        for aid in ("a1;rm", "a b", "$(id)", "", "a" * 81):
            with self.subTest(aid=aid):
                self.assertIsNone(laneroute.route("cd lib && dev progress step x", aid))

    def test_dev_is_named_as_a_word_or_a_literal_path(self):
        self.assertEqual([bool(laneroute.LITERAL_DEV.match(w)) for w in ("dev", "bin/dev", "~/x/dev", "devx", "$D")],
                         [True, True, True, False, False])


class HoldsLaneCallTests(unittest.TestCase):

    def test_a_lane_call_is_found_at_any_depth_the_reader_reaches(self):
        for cmd in ("dev progress step x", "bash -c 'dev progress step x'", "echo $(dev crew inbox)",
                    "python3 -c 'import subprocess; subprocess.run([\"dev\", \"crew\", \"ask\", \"x\"])'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(laneroute.holds_lane_call(cmd, CWD))

    def test_text_that_only_mentions_one_is_no_call(self):
        for cmd in ("echo 'dev progress step x'", "git commit -m 'dev crew ask y'", "dev crew brief tank",
                    "python3 -c 'print(\"dev progress step\")'"):
            with self.subTest(cmd=cmd):
                self.assertFalse(laneroute.holds_lane_call(cmd, CWD))


class HookRoutingTests(unittest.TestCase):
    # The PreToolUse hook (hooks._route_subagent_progress) on a member's call, its lane mocked: a review member (the
    # Code reviewer) and a write member (the DevEx engineer). A call the router places gets the variable there; a
    # compound command that holds lane calls is stamped as a whole on Claude Code, whose rewrite approves nothing, so
    # each of its dev calls acts as the member; Codex, whose rewrite would approve it, keeps the router's placement.
    STAMP = "export DEVKIT_FROM_AGENT=a1; readonly DEVKIT_FROM_AGENT; "

    def lane(self, persona, level):
        from devkit import progress
        run = {"agents": {"a1": {"persona": persona}}}
        for name, value in (("lane_of", lambda *a: (run, run["agents"]["a1"])), ("lane_chain", lambda r, a: [a]),
                            ("lane_tools", lambda r, a: level), ("lane_scope", lambda r, a: None)):
            p = mock.patch.object(progress, name, value)
            p.start()
            self.addCleanup(p.stop)

    def data(self, cmd):
        return {"agent_id": "a1", "tool_name": "Bash", "session_id": "s", "cwd": CWD, "tool_input": {"command": cmd}}

    def test_a_recorded_lane_call_is_routed_in_a_review_and_a_write_lane(self):
        from devkit import hooks
        for persona, level in (("Seraph", "review"), ("Kid", "all")):
            self.lane(persona, level)
            for cmd, where in ROUTED:
                with self.subTest(cmd=cmd, persona=persona):
                    self.assertIn(where, (hooks._route_subagent_progress(self.data(cmd)) or {}).get("command", ""))

    def test_a_compound_command_with_lane_calls_is_stamped_for_the_member(self):
        from devkit import hooks
        for persona, level in (("Kid", "all"), ("Seraph", "review")):
            self.lane(persona, level)
            for cmd in ('dev progress step "a" --check "x"; dev progress step "b" --check "y"',
                        "NO_COLOR=1 dev crew inbox", "for s in a b; do dev progress note \"$s\"; done",
                        "dev crew inbox 2>&1 | tail -15; dev progress note y", "(cd lib && dev progress note x)",
                        "dev progress note x &", "bash -c 'dev crew inbox'"):
                with self.subTest(cmd=cmd, persona=persona):
                    self.assertEqual((hooks._route_subagent_progress(self.data(cmd)) or {}).get("command"),
                                     self.STAMP + cmd)
                    self.assertIsNone(hooks._route_subagent_progress(self.data(cmd), "codex"))

    def test_a_review_member_waits_and_reads_its_inbox(self):
        # the wait every member is told to run when it is blocked on an answer, and its inbox, in a review lane
        from devkit import hooks
        self.lane("Seraph", "review")
        for cmd in ("dev watch --wait --for me --timeout 5", "dev crew inbox", "dev crew inbox --all"):
            with self.subTest(cmd=cmd):
                self.assertEqual((hooks._route_subagent_progress(self.data(cmd)) or {}).get("command"), HEAD + cmd)

    def test_the_stamp_approves_nothing(self):
        # Claude Code's rewrite adds the variable and no permission decision: the harness's own permission check runs
        # on the stamped command
        from devkit import hooks
        self.lane("Kid", "all")
        for cmd in ("dev progress step x; dev progress note y", "dev progress step x"):
            with self.subTest(cmd=cmd):
                rewrite = hooks._rewrite("claude", hooks._route_subagent_progress(self.data(cmd)))
                self.assertNotIn("permissionDecision", json.dumps(rewrite))
                self.assertIn("DEVKIT_FROM_AGENT=a1", json.dumps(rewrite))


if __name__ == "__main__":
    unittest.main()
