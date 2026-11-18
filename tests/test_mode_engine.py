# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The mode engine behind devkit's auto and guided work modes: which mode a run's decisions follow, what a switch of
# mode does to the decisions that wait, what the session start, the continuity brief and devkit's notices say about
# it, and the commands devkit tells agents to run for an approval. Every test runs the real code in test_crew's
# sandbox (progress state, the audit trail and devkit's configuration file in a temporary folder) and, on top of it,
# points core.STATE and core.CONFIG_FILE, and every devkit module's binding of them, at the test's own folder: nothing
# here reads or writes the developer's ~/.config/devkit or ~/.local/state/devkit. Tests carry comments, not
# docstrings: `dev test prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_mode_engine
import json
import os
import re
import shlex
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _hermetic  # noqa: E402

_hermetic.activate()

sys.path.insert(0, str(HERE.parent / "lib"))
from devkit import approvals, audit, cli, configschema, continuity, core, crew, dispatch, mcp  # noqa: E402
from devkit import progress as pg  # noqa: E402
import test_hooks_welcome  # noqa: E402
from test_crew import TempState, run_cli  # noqa: E402
from test_progress import launch  # noqa: E402

AUTO = (configschema.effective({})[0], [])
GUIDED = (configschema.effective({"work": {"mode": "guided"}})[0], [])
INVALID = (configschema.effective({})[0], ["work.mode must be one of auto, guided"])
# where devkit's state and configuration live in this run (the suite's temporary home), before any test moves them
BOUND = {"STATE": core.STATE, "CONFIG_FILE": core.CONFIG_FILE}


def below(path, root) -> bool:
    p, r = os.path.realpath(str(path)), os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


def seal(case, root: Path, state_dir: Path):
    # core.STATE and core.CONFIG_FILE, and each devkit module that bound either at import, below the test's folder
    targets = {"STATE": state_dir, "CONFIG_FILE": root / "config" / "config.json"}
    for attr, target in targets.items():
        for name, module in sorted(sys.modules.items()):
            if (name == "devkit" or name.startswith("devkit.")) and getattr(module, attr, None) == BOUND[attr]:
                p = mock.patch.object(module, attr, target)
                p.start()
                case.addCleanup(p.stop)
    for attr in targets:
        if not below(getattr(core, attr), root):
            case.fail(f"core.{attr} is {getattr(core, attr)}, outside the test's folder {root}: refusing to run")


def use(case, snapshot):
    # the saved configuration devkit reads from now on in this test: auto, guided or invalid
    p = mock.patch.object(core, "config_snapshot", return_value=snapshot)
    p.start()
    case.addCleanup(p.stop)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        seal(self, self.root, self.root / "state")

    @staticmethod
    def run_():
        return pg.load_run("claude", "s1")

    @staticmethod
    def edit_run(change):
        path = pg.run_path("claude", "s1")
        run = json.loads(path.read_text())
        change(run)
        path.write_text(json.dumps(run))

    @staticmethod
    def neo_works_on_the_parser():
        pg.assign("parser", "Neo")
        launch("a1", "devkit:neo", "Neo: parser")

    @staticmethod
    def neo_asks(*more):
        return run_cli("crew", "ask", "--decision", "Skip empty lines or reject the file?",
                       "--options", "A: skip them | B: reject the file", "--recommend", "A", *more, from_agent="a1")

    @staticmethod
    def audit_records(action):
        return [r for f in sorted(audit.AUDIT.glob("*.jsonl")) for r in map(json.loads, f.read_text().splitlines())
                if r.get("action") == action]


class ApprovalDecisionTests(EngineCase):
    # auto mode never answers a decision that approves steps: only the developer's answer approves them, so it waits
    # for the developer and the approval it carries can be given
    def test_auto_leaves_a_decision_that_approves_steps_to_the_developer(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        d = pg.crew_decision("Approve build?", options="A: approve | B: changes", recommend="A", approves="build")
        self.assertEqual(("pending", "", ""), (d["state"], d["answer"], d["by"]))
        self.assertEqual(["D1"], [x["id"] for x in pg.pending_decisions(self.run_())])

    # `dev crew ask --decision --approves` in auto mode says the decision waits for the developer, and why
    def test_auto_says_a_decision_that_approves_steps_waits_for_the_developer(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        rc, out, err = run_cli("crew", "ask", "--decision", "Approve build?", "--options", "A: approve | B: changes",
                               "--recommend", "A", "--approves", "build")
        self.assertEqual(0, rc, err)
        self.assertNotIn("go ahead", out)
        self.assertIn("only the developer's answer approves", out)


class ReplaceTests(EngineCase):
    # `dev progress start --replace` without --mode keeps the mode the tech lead set for the live run (dev crew mode),
    # and a run that followed the configuration hands that on: the new run follows the configuration too
    def test_replace_without_mode_keeps_only_an_explicit_run_mode(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        pg.set_mode("guided")
        pg.start("Ship again", plan="build=30m", replace=True, why="re-plan")
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))
        pg.set_mode("inherit")
        pg.start("Ship a third time", plan="build=30m", replace=True, why="re-plan")
        self.assertEqual(("auto", "config"), pg.mode_context(self.run_()))

    # a run saved before the mode had an origin keeps its mode when it holds `approved`, which only `dev crew mode`
    # wrote then: replacing it keeps that explicit choice
    def test_replace_without_mode_keeps_a_legacy_run_s_explicit_mode(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        self.edit_run(lambda r: (r.pop("mode_source", None), r.update(mode="guided", approved=[])))
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))
        pg.start("Ship again", plan="build=30m", replace=True, why="re-plan")
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))


class ContinuityTests(EngineCase):
    # the continuity brief of a live run says the mode its decisions follow now, not the one recorded at its start
    def test_the_brief_shows_the_mode_the_live_run_follows(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        use(self, GUIDED)
        self.assertIn("(running, guided mode)", continuity.brief("claude", "s1"))

    # the checkpoint keeps the mode the run followed when it was written, and the brief shows it when the live run
    # cannot be read
    def test_the_checkpoint_keeps_the_mode_the_run_followed(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        use(self, GUIDED)
        continuity.on_pre_compact("claude", {"session_id": "s1", "cwd": str(self.root), "trigger": "manual"})
        pg.run_path("claude", "s1").unlink()
        self.assertIn("(running, guided mode)", continuity.brief("claude", "s1"))


class SwitchToAutoTests(EngineCase):
    # `dev crew mode auto` takes each waiting decision with a recommendation as auto mode would have: decided by
    # auto, the member that asked works again and gets the answer, and the audit trail and the run's history record
    # it; a decision that approves steps keeps waiting for the developer
    def test_switching_to_auto_takes_what_auto_would_have_taken(self):
        use(self, GUIDED)
        pg.start("Parser", plan=["parser", "build"])
        self.neo_works_on_the_parser()
        self.assertEqual(0, self.neo_asks()[0])
        pg.crew_decision("Approve the parser?", options="A: approve | B: changes", recommend="A", approves="parser")
        pg.set_mode("auto")
        run = self.run_()
        taken, approval = run["decisions"]
        self.assertEqual(("decided", "A", "auto"), (taken["state"], taken["answer"], taken["by"]))
        self.assertEqual(("pending", ""), (approval["state"], approval["by"]))
        self.assertEqual("running", run["plan"][0]["state"])
        self.assertNotIn("waiting", run["agents"]["a1"])
        self.assertTrue(any(m.get("to") == "a1" and "A: skip them" in (m.get("text") or "")
                            for m in run.get("messages") or []), run.get("messages"))
        self.assertEqual([("D1", "auto")], [(r.get("target"), (r.get("ref") or {}).get("by"))
                                            for r in self.audit_records("crew.decided")])
        self.assertIn(("(decision D1)", "decided"), [(h.get("step"), h.get("to")) for h in run["history"]])

    # the command says which decisions it took, with the option
    def test_dev_crew_mode_auto_says_what_it_took(self):
        use(self, GUIDED)
        pg.start("Parser", plan=["parser", "build"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        rc, out, err = run_cli("crew", "mode", "auto")
        self.assertEqual(0, rc, err)
        self.assertIn("D1", out)
        self.assertIn("A: skip them", out)

    # `dev crew mode inherit` records the mode it resolves to, next to inherit, in the audit trail and the history
    def test_inherit_records_the_mode_it_resolves_to(self):
        use(self, GUIDED)
        pg.start("Parser", plan=["parser"])
        pg.set_mode("auto")
        pg.set_mode("inherit")
        rec = self.audit_records("crew.mode")[-1]
        self.assertEqual(("inherit", "guided"), (rec.get("target"), (rec.get("ref") or {}).get("mode")))
        last = [h for h in self.run_()["history"] if h.get("step") == "(mode)"][-1]
        self.assertEqual(("inherit", "guided"), (last.get("to"), last.get("resolved")))


class InvalidConfigurationTests(EngineCase):
    # a configuration devkit cannot read never decides for the developer and never refuses the work: decisions wait
    # for the developer, as in guided mode
    def test_an_invalid_configuration_reads_as_guided(self):
        self.assertEqual(("guided", "invalid configuration"), pg.mode_context(None, INVALID))

    # a run starts, and a decision is asked, while the configuration is invalid: the decision waits
    def test_a_decision_under_an_invalid_configuration_waits_for_the_developer(self):
        use(self, INVALID)
        pg.start("Ship", plan="build=30m", mode="guided")
        pg.set_mode("inherit")
        d = pg.crew_decision("Hosted or own?", options="A: hosted | B: own", recommend="A")
        self.assertEqual(("pending", ""), (d["state"], d["by"]))

    # the views still say the configuration is invalid, and that decisions wait for the developer until it is fixed
    def test_the_views_say_invalid_and_that_decisions_wait(self):
        use(self, AUTO)
        pg.start("Ship", plan="build=30m")
        use(self, INVALID)
        view = crew.run_view(self.run_())
        self.assertEqual("invalid", view["mode"])
        line = crew.mode_line(view)
        self.assertIn("wait for the developer", line)
        self.assertIn("dev config validate", line)


class ToldOnceTests(EngineCase):
    # a decision is marked told only when its notice is delivered: a run whose mode cannot be read still delivers it
    def test_a_decision_is_never_marked_told_without_being_delivered(self):
        use(self, GUIDED)
        pg.start("Ship", plan="build=30m")
        pg.crew_decision("Hosted or own?", options="A: hosted | B: own")
        self.edit_run(lambda r: r.update(mode_source="unknown origin"))
        try:
            text = pg.decisions_context("claude", "s1")
        except pg.StepError:
            text = ""
        self.assertIn("D1", text)

    # a ready step is marked told only when its notice is delivered, by the same rule
    def test_a_ready_step_is_never_marked_told_without_being_delivered(self):
        use(self, GUIDED)
        pg.start("Ship", plan="build=30m; docs=10m")
        pg.assign("docs", "Ghost")
        self.edit_run(lambda r: r.update(mode_source="unknown origin"))
        try:
            text = dispatch.ready_context("claude", "s1")
        except pg.StepError:
            text = ""
        self.assertIn("dev crew brief ghost", text)


class ApprovalHintTests(unittest.TestCase):
    # the command devkit gives for an approval by the developer exists, and passes the decision's shape check
    def assert_runnable(self, text):
        m = re.search(r"dev crew ask --decision .*?--approves \"[^\"]*\"", text)
        self.assertIsNotNone(m, text)
        argv = shlex.split(m.group(0))[1:]
        a = cli.build_parser().parse_args(argv)
        question = " ".join(a.text) if isinstance(a.text, list) else a.text
        pg.decision_shape(question, a.options, a.recommend, a.approves)

    def test_the_run_end_hint_for_the_developer_s_approval_is_a_runnable_decision(self):
        self.assert_runnable(approvals.message([(approvals.DEVELOPER, ["build", "docs"])]))

    def test_the_refusal_of_a_developer_approval_names_a_runnable_decision(self):
        with self.assertRaises(approvals.ApprovalError) as refused:
            approvals.record({"plan": [{"name": "build", "state": "done", "finished": 1}]}, approvals.DEVELOPER,
                             "build", "approved", time.time())
        self.assert_runnable(str(refused.exception))

    # the module names no command that does not exist, and no labels of the plan that produced it (a spec, review
    # batch or finding id), in its code, comments or docstrings
    def test_the_approvals_module_names_no_missing_command_and_no_plan_label(self):
        source = Path(approvals.__file__).read_text()
        self.assertNotRegex(source, r"dev crew decision\b")
        self.assertNotRegex(source, r"\bspec\b|\bbatch \d|\bR\d-\d|\b[CF]\d+\b")


class McpTemplateTests(unittest.TestCase):
    # the You bullet the chat surfaces read, as the docs say it: an action only the developer can take, each open
    # decision in guided mode, and in both modes each decision that approves a step (only their answer approves one)
    def test_the_you_bullet_holds_actions_open_decisions_in_guided_mode_and_approvals_in_both(self):
        self.assertIn("- **You:** <only what the developer alone can do: an action on their machine or account; in "
                      "guided mode, each open decision; in both modes, each decision that approves a step>",
                      mcp.INSTRUCTIONS)


class SessionStartTests(test_hooks_welcome.WelcomeHook):
    def setUp(self):
        super().setUp()
        seal(self, self.root, self.root)

    def own_run(self, mode: str, source: str = "run", decision: bool = True, recommend: str = "", approves=None):
        # this session (s9) resumes its own run, with a decision of the tech lead's that waits for the developer
        now = time.time()
        pg.RUNS.mkdir(parents=True, exist_ok=True)
        decisions = [dict({"id": "D1", "from": "main", "from_name": "Morpheus", "question": "Hosted or own?",
                           "options": ["A: hosted", "B: own"], "recommend": recommend, "state": "pending",
                           "answer": "", "by": "", "ts": now - 60}, **({"approves": approves} if approves else {}))
                     ] if decision else []
        pg.run_path("claude", "s9").write_text(json.dumps(
            {"v": 2, "agent": "claude", "session": "s9", "project": self.pid, "goal": "Invoice export",
             "state": "running", "started": now - 600, "updates": [], "intervals": [], "agents": {}, "messages": [],
             "plan": [{"name": "build", "state": "running", "owner": "main", "est": 10, "started": now - 600}],
             "decisions": decisions, "mode": mode, "mode_source": source}))

    def context(self, **more) -> str:
        return self.start("claude", "s9", **more)["hookSpecificOutput"]["additionalContext"]

    # another session's decisions are asked in that session: this one is never told to ask them first
    def test_the_note_never_asks_another_session_s_decisions(self):
        use(self, GUIDED)
        self.live_run()
        self.assertNotIn("ask the waiting decisions", self.context())

    # the note asks this session's own decisions first exactly when one waits for the developer: in guided mode every
    # pending one; in auto mode only one auto cannot take, that approves steps or has no recommendation
    def test_the_note_asks_own_decisions_first_only_when_one_waits_for_the_developer(self):
        cases = ((GUIDED, "guided", {"recommend": "A"}, True),
                 (AUTO, "auto", {"recommend": "A"}, False),
                 (AUTO, "auto", {"recommend": "A", "approves": ["build"]}, True),
                 (AUTO, "auto", {"recommend": ""}, True))
        for saved, mode, decision, asks in cases:
            with self.subTest(mode=mode, **decision):
                use(self, saved)
                self.own_run(mode, source="config", **decision)
                self.assertEqual(asks, "ask the waiting decisions first" in self.context(source="resume"))

    # the session start says the mode decisions follow, in one line: here the run's own, over the configuration's
    def test_the_context_says_the_current_mode_in_one_line(self):
        use(self, AUTO)
        self.own_run("guided", decision=False)
        self.assertEqual(1, self.context(source="resume").count("Mode guided (run)"))

    # records stay in American English, and replies follow the developer's language
    def test_the_context_says_replies_follow_the_developer_s_language(self):
        ctx = self.context()
        self.assertNotIn("plain American English", ctx)
        self.assertIn("replies follow the developer's language", ctx)


if __name__ == "__main__":
    unittest.main()
