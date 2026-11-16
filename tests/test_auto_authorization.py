# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Automatic policy integration at the existing mode and grant boundaries."""
import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_progress import TempState
from devkit import core, guard, progress as pg


class ModeResolutionTests(unittest.TestCase):
    def test_inherited_run_observes_fresh_configuration(self):
        run = {"mode": "auto", "mode_source": "config"}
        with mock.patch.object(core, "config_snapshot", create=True, return_value=({"work": {"mode": "guided"}}, [])):
            self.assertEqual(pg.mode_of(run), "guided")

    def test_absent_configuration_uses_auto(self):
        with mock.patch.object(core, "config_snapshot", create=True, return_value=({}, [])):
            self.assertEqual(pg.mode_of(None), "auto")

    def test_invalid_configuration_denies_even_explicit_auto(self):
        with mock.patch.object(core, "config_snapshot", create=True, return_value=({}, ["work.mode: invalid"])):
            with self.assertRaises(pg.StepError):
                pg.mode_of({"mode": "auto", "mode_source": "run"})

    def test_explicit_and_legacy_modes_survive_saved_change(self):
        with mock.patch.object(core, "config_snapshot", create=True, return_value=({"work": {"mode": "auto"}}, [])):
            self.assertEqual(pg.mode_context({"mode": "guided", "mode_source": "run"}), ("guided", "run"))
            self.assertEqual(pg.mode_context({"mode": "guided"}), ("guided", "legacy"))

    def test_finished_run_no_longer_overrides_saved_policy(self):
        with mock.patch.object(core, "config_snapshot", create=True, return_value=({"work": {"mode": "guided"}}, [])):
            self.assertEqual(pg.mode_of({"mode": "auto", "mode_source": "run", "state": "done"}), "guided")

    def test_start_records_inheritance_and_inherit_clears_override(self):
        with TempState(), mock.patch.object(core, "config_snapshot", create=True, return_value=({"work": {"mode": "auto"}}, [])):
            run = pg.start("Ship", plan=["build"])
            self.assertEqual(run.get("mode_source"), "config")
            pg.set_mode("guided")
            inherited = pg.set_mode("inherit")
            self.assertEqual((inherited["mode_source"], pg.mode_of(inherited)), ("config", "auto"))

    def test_unverified_command_cannot_change_run_authority(self):
        from types import SimpleNamespace
        from devkit import identity
        with TempState():
            pg.start("Work", plan=["build"])
            missing = SimpleNamespace(ledger="", calls=[], members=[], session_how="recency",
                                      speaker=SimpleNamespace(lane="main", how="unverified"))
            with mock.patch.object(pg, "caller", return_value={"caller": "unverified", "agent": "codex"}), \
                    mock.patch.object(identity, "_find", return_value=missing):
                with self.assertRaises(pg.StepError):
                    pg.set_mode("auto")
                with self.assertRaises(pg.StepError):
                    pg.start("Replace", plan=["build"], mode="auto", replace=True, why="change authority")
            self.assertEqual(pg.mode_of(pg.load_run("claude", "s1")), "guided")


class AutomaticReceiptTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.config = {"work": {"mode": "auto"}}
        patch = mock.patch.object(core, "config_snapshot", create=True, side_effect=lambda: (self.config, []))
        patch.start()
        self.addCleanup(patch.stop)
        self.real_sh = core.sh
        network = mock.patch.object(core, "sh", side_effect=lambda argv, **kw:
                                    (0, '{"login":"alice","id":123}', "") if argv[0] == "gh"
                                    else self.real_sh(argv, **kw))
        network.start()
        self.addCleanup(network.stop)

    def use(self, **kw):
        return pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo="acme/api", **kw)

    def test_auto_receipt_is_exact_and_never_human_evidence(self):
        before = self.state.sess.read_bytes()
        receipt = self.use(consume=False)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["source"], "policy-auto")
        self.assertEqual((receipt["action"], receipt["target"], receipt["repo"]),
                         ("release", "v26.11.4", "acme/api"))
        self.assertIsNone(receipt["expires"])
        self.assertIsNone(receipt["uses_left"])
        self.assertEqual(self.state.sess.read_bytes(), before)
        self.assertIsNotNone(self.use())
        self.assertNotIn("human", json.loads(self.state.sess.read_text()))

    def test_consumption_reloads_mode_after_auto_peek(self):
        self.assertIsNotNone(self.use(consume=False))
        self.config["work"]["mode"] = "guided"
        self.assertIsNone(self.use())

    def test_invalid_config_precedes_headless_preapproval(self):
        with mock.patch.object(core, "config_snapshot", return_value=({}, ["unknown key"])), \
                mock.patch.object(pg, "headless", return_value=True), \
                mock.patch.object(pg, "preapproved", return_value=True):
            self.assertIsNone(self.use())

    def test_supported_operations_have_policy_receipts(self):
        for action, target in (("merge", "12"), ("push", "feat/example"), ("pr", "feat/example"),
                               ("api-write", "repos/acme/api/issues/7"), ("backup", "acme/api")):
            with self.subTest(action=action):
                receipt = pg.use_grant("claude", "s1", action=action, target=target, repo="acme/api")
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt["source"], "policy-auto")

    def test_the_developers_own_decision_classes_never_get_a_policy_receipt(self):
        # auto mode authorizes routine operations; these stay the developer's typed grant or approved decision
        for action, target in (("embargo", "fix/example"), ("bug-send", "7"), ("bug-drop", "7"),
                               ("bug-confirm", "7"), ("bug-share", "7"), ("lesson-release", "L-example"),
                               ("lesson-block", "L-example")):
            with self.subTest(action=action):
                decision = pg.authorization_decision("claude", "s1", action=action, target=target, repo="acme/api")
                self.assertEqual(decision["outcome"], "require-human")
                self.assertIsNone(pg.use_grant("claude", "s1", action=action, target=target, repo="acme/api"))

    def test_guard_invalid_configuration_denies_before_tier_one_and_audit(self):
        action = {"action": "push", "target": "feat/example", "repo": "acme/api", "tier": 1,
                  "words": "git push origin feat/example"}
        with mock.patch.object(core, "config_snapshot", return_value=({}, ["unknown key"])), \
                mock.patch.object(guard, "irreversible_setting", return_value="audit"):
            self.assertIsNotNone(guard._main_gate([action], "claude", "s1", None, None))

    def test_an_invalid_configuration_refuses_irreversible_commands_but_not_ordinary_ones(self):
        # the developer can still read, test and repair the configuration while it is invalid; what it refuses is
        # authority for irreversible actions
        cwd = os.getcwd()
        with mock.patch.object(core, "config_snapshot", return_value=({}, ["unknown key legacy_root"])):
            for cmd in ("ls -la", "dev config validate", "python3 -m unittest tests.test_state"):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1"))
            denied = guard.irreversible_verdict("gh pr merge 12 -R acme/api", cwd, agent="claude", session="s1")
        self.assertIsNotNone(denied)
        self.assertIn("Invalid policy configuration", denied[1])

    def test_guard_auto_unknown_needs_its_exact_command_digest(self):
        # policy authorizes one exact unclassified command, named by its digest; the audit week never stands in
        action = {"action": "unknown", "target": "dynamic", "repo": "acme/api", "tier": 2,
                  "words": "dynamic-command"}
        with mock.patch.object(guard, "irreversible_setting", return_value="audit"):
            self.assertIsNotNone(guard._main_gate([action], "claude", "s1", None, None))
            self.assertIsNone(guard._main_gate([dict(action, target="cmd-0123456789")], "claude", "s1", None,
                                               None))

    def test_mcp_auto_requires_resource_instead_of_tool_name(self):
        with mock.patch.object(guard, "irreversible_setting", return_value="audit"):
            denied = guard.irreversible_tool_verdict("mcp__slack__send_message", {"text": "hello"},
                                                     "/repo", agent="claude", session="s1")
        self.assertIsNotNone(denied)
        self.assertIn("supported", denied[1])

    def test_guided_compound_duplicate_grant_does_not_spend_first_use(self):
        from tests.test_evidence import EvidenceCase, m
        fixture = EvidenceCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config["work"]["mode"] = "guided"
        fixture.typed("grant release v26.11.4", m(1))
        action = {"action": "release", "target": "v26.11.4", "repo": "acme/api", "tier": 2,
                  "words": "dev release --version 26.11.4"}
        with mock.patch.object(guard, "irreversible_setting", return_value="enforce"):
            self.assertIsNotNone(guard._main_gate([action, action], "claude", "s1", None, m(2)))
        self.assertIsNotNone(pg.use_grant("claude", "s1", action="release", target="v26.11.4",
                                        repo="acme/api", consume=False, now=m(2)))

    def test_classifier_preserves_repeated_executions(self):
        actions = guard.irreversible_actions("gh release create v26.11.4 -R acme/api; "
                                             "gh release create v26.11.4 -R acme/api", "/repo")
        self.assertEqual(len(actions), 2)

    def test_classifier_preserves_nested_repeated_executions(self):
        for command in ("bash -c 'gh pr merge 12; gh pr merge 12'",
                        "echo $(gh pr merge 12); gh pr merge 12",
                        "echo $(gh pr merge 12) $(gh pr merge 12)"):
            with self.subTest(command=command):
                self.assertEqual(len(guard.irreversible_actions(command, "/repo")), 2)

    def test_provider_hostname_binds_the_operation_account(self):
        actions = guard.irreversible_actions(
            "gh api repos/acme/api/issues/7 -X PATCH --hostname git.example.com", "/repo")
        self.assertEqual(actions[0]["repo"], "git.example.com/acme/api")
        session = json.loads(self.state.sess.read_text())
        self.state.sess.write_text(json.dumps(dict(session, project="git.example.com/acme/api")))
        with mock.patch.object(core, "sh", return_value=(0, '{"login":"alice","id":123}', "")) as network:
            self.assertIsNone(guard._main_gate(actions, "claude", "s1", None, None))
        self.assertIn("git.example.com", network.call_args.args[0])

    def test_provider_repository_hostname_is_not_discarded(self):
        actions = guard.irreversible_actions("gh pr merge 12 -R git.example.com/acme/api", "/repo")
        self.assertEqual(actions[0]["repo"], "git.example.com/acme/api")

    def test_automatic_cleanup_applies_exact_plan_without_prompt(self):
        from types import SimpleNamespace
        from devkit import cli
        plan = {"actions": [{"kind": "prune-artefacts", "project": "acme/api", "path": "/repo/build",
                              "reason": "unused build output", "auto": True}], "notes": [], "reclaimable_kb": 1}
        args = SimpleNamespace(apply=True, yes=False, verbose=False, auto_only=False)
        with mock.patch.object(cli.registry, "build", return_value={}), \
                mock.patch.object(cli.gc, "plan", return_value=plan), \
                mock.patch.object(cli.gc, "apply", return_value=[]) as apply, \
                mock.patch.object(cli, "cmd_sync", return_value=0), \
                mock.patch("builtins.input", side_effect=AssertionError("automatic cleanup cannot prompt")):
            self.assertEqual(cli._gc_unattended(args), 0)
        apply.assert_called_once_with(plan, only_auto=False, remote=True)

    def test_developer_terminal_cleanup_yes_needs_no_agent_session(self):
        from types import SimpleNamespace
        from devkit import cli
        plan = {"actions": [{"kind": "prune-artefacts", "project": "acme/api", "path": "/fixture/build",
                              "reason": "unused build output", "auto": True}], "notes": [], "reclaimable_kb": 1}
        args = SimpleNamespace(apply=True, yes=True, verbose=False, auto_only=True)
        with mock.patch.object(cli.registry, "build", return_value={}), \
                mock.patch.object(cli.gc, "plan", return_value=plan), \
                mock.patch.object(cli.gc, "apply", return_value=[]) as apply, \
                mock.patch.object(cli, "cmd_sync", return_value=0), \
                mock.patch.object(pg, "current_session", return_value=("cli", "")), \
                mock.patch.object(core, "set_by_developer", return_value=True):
            self.assertEqual(cli._gc_unattended(args), 0)
        apply.assert_called_once_with(plan, only_auto=True, remote=True)

    def cleanup_command(self, *, yes=True):
        from types import SimpleNamespace
        from devkit import cli
        plan = {"actions": [{"kind": "prune-artefacts", "project": "acme/api", "path": "/fixture/build",
                              "reason": "unused build output", "auto": True}], "notes": [], "reclaimable_kb": 1}
        with mock.patch.object(cli.registry, "build", return_value={}), \
                mock.patch.object(cli.gc, "plan", return_value=plan), \
                mock.patch.object(cli.gc, "apply", return_value=[]) as apply, \
                mock.patch.object(cli, "cmd_sync", return_value=0):
            rc = cli._gc_unattended(SimpleNamespace(apply=True, yes=yes, verbose=False, auto_only=True))
        return rc, apply.call_count

    def test_cleanup_terminal_still_requires_confirmation_in_auto(self):
        from devkit import cli
        with mock.patch.object(core, "set_by_developer", return_value=True), \
                mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            self.assertEqual(self.cleanup_command(yes=False), (2, 0))

    def test_cleanup_unknown_or_member_cannot_use_yes_in_either_mode(self):
        for mode in ("auto", "guided"):
            self.config["work"]["mode"] = mode
            for caller in ("unknown", "neo"):
                with self.subTest(mode=mode, caller=caller), \
                        mock.patch.object(core, "set_by_developer", return_value=False), \
                        mock.patch.object(pg, "caller", return_value={"caller": caller}), \
                        mock.patch.dict(os.environ, {"DEVKIT_JOB": "gc", "DEVKIT_AGENT": "job"}):
                    self.assertEqual(self.cleanup_command(), (2, 0))

    def test_guided_cleanup_preserves_registered_main_hook_grant_contract(self):
        self.config["work"]["mode"] = "guided"
        with mock.patch.object(core, "set_by_developer", return_value=False), \
                mock.patch.object(pg, "authorize_cleanup", side_effect=AssertionError("hook already consumed grant")):
            self.assertEqual(self.cleanup_command(), (0, 1))
        self.state.sess.unlink()
        with mock.patch.object(core, "set_by_developer", return_value=False):
            self.assertEqual(self.cleanup_command(), (2, 0))

    def test_cleanup_launchd_job_uses_independent_parent_proof(self):
        from devkit import jobs
        script = str(jobs.DEVKIT_HOME / "bin" / "dev")
        parent = shlex.join([sys.executable, "-I", "-S", script, "jobs", "exec", "gc"])
        snapshot = {os.getppid(): (1, parent), 1: (0, "/sbin/launchd")}
        service = '{\n\t"Label" = "com.devkit.job.gc";\n\t"PID" = ' + str(os.getppid()) + ';\n};\n'
        with mock.patch.dict(os.environ, {"DEVKIT_AGENT": "job", "DEVKIT_JOB": "gc"}, clear=True), \
                mock.patch.object(sys, "argv", [script, "gc", "--apply", "--yes", "--auto-only"]), \
                mock.patch.object(core, "set_by_developer", return_value=False), \
                mock.patch.object(core, "sh", return_value=(0, service, "")) as service_query, \
                mock.patch.object(pg, "current_session", return_value=("cli", "")), \
                mock.patch.object(pg, "_ps", return_value=snapshot):
            self.assertEqual(self.cleanup_command(), (0, 1))
            for override in ({"jobs": {"gc": {"enabled": False}}}, {"gc": {"auto": False}},
                             {"jobs": {"gc": {"cmd": ["gc", "--apply", "--yes"]}}}):
                with self.subTest(override=override):
                    self.config = {"work": {"mode": "auto"}, **override}
                    self.assertEqual(self.cleanup_command(), (2, 0))
            self.config = {"work": {"mode": "auto"}, "gc": {"auto": False}, "jobs": {"gc": {"enabled": True}}}
            self.assertEqual(self.cleanup_command(), (0, 1))
            for evidence in ((1, "", "service not found"), (0, "", ""),
                             (0, service.replace(str(os.getppid()), "999999"), ""),
                             (0, service.replace('"PID"', '"NotPID"'), ""),
                             (0, service.replace("com.devkit.job.gc", "com.devkit.job.sleep"), ""),
                             (0, service + service, ""), (0, service * 2000, ""),
                             (0, service.replace(str(os.getppid()), "invalid"), "")):
                with self.subTest(service=evidence[0:1] + (evidence[1][:100],)):
                    service_query.return_value = evidence
                    self.assertEqual(self.cleanup_command(), (2, 0))
            service_query.return_value = (0, service, "")
            for failure in (OSError("launchctl unavailable"), subprocess.TimeoutExpired("launchctl", 5)):
                with mock.patch.object(core, "sh", side_effect=failure):
                    self.assertEqual(self.cleanup_command(), (2, 0))
            self.assertEqual(service_query.call_args, mock.call(["/bin/launchctl", "list", "com.devkit.job.gc"], timeout=5))
            with mock.patch.object(core, "config_snapshot", return_value=({"jobs": "invalid"}, ["jobs invalid"])):
                self.assertFalse(jobs.authenticated_gc_job())

    def test_cleanup_launchd_job_accepts_the_homebrew_framework_python_it_runs_as(self):
        # Homebrew's python3.15 is a framework build: ps shows the launchd job's parent as the framework's
        # Python.app executable, never as the bin/python3.15 that sys.executable names
        import tempfile
        from pathlib import Path
        from devkit import jobs
        root = Path(tempfile.mkdtemp(dir=str(pg.RUNS.parent)))
        keg = root / "Cellar" / "python@3.15" / "3.15.2"
        version = keg / "Frameworks" / "Python.framework" / "Versions" / "3.15"
        app = version / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        for binary in (version / "bin" / "python3.15", app):
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("")
        (keg / "bin").mkdir()
        (keg / "bin" / "python3.15").symlink_to("../Frameworks/Python.framework/Versions/3.15/bin/python3.15")
        script = str(jobs.DEVKIT_HOME / "bin" / "dev")
        service = '{\n\t"LimitLoadToSessionType" = "Aqua";\n\t"Label" = "com.devkit.job.gc";\n\t"OnDemand" = true;\n' \
                  '\t"LastExitStatus" = 0;\n\t"PID" = ' + str(os.getppid()) + ';\n\t"Program" = "' + str(app) + '";\n};\n'

        def parent(executable):
            return {os.getppid(): (1, f"{executable} -I -S {script} jobs exec gc"), 1: (0, "/sbin/launchd")}
        with mock.patch.dict(os.environ, {"DEVKIT_AGENT": "job", "DEVKIT_JOB": "gc"}, clear=True), \
                mock.patch.object(sys, "argv", [script, "gc", "--apply", "--yes", "--auto-only"]), \
                mock.patch.object(sys, "executable", str(keg / "bin" / "python3.15")), \
                mock.patch.object(sys, "base_prefix", str(version)), \
                mock.patch.object(core, "sh", return_value=(0, service, "")), \
                mock.patch.object(pg, "_ps", return_value=parent(app)) as ps:
            self.assertTrue(jobs.authenticated_gc_job())
            other = "/Library/Frameworks/Python.framework/Versions/3.15/Resources/Python.app/Contents/MacOS/Python"
            ps.return_value = parent(other)
            self.assertFalse(jobs.authenticated_gc_job())

    def test_cleanup_launchd_rejects_forged_or_incomplete_process_evidence(self):
        from devkit import jobs
        script = str(jobs.DEVKIT_HOME / "bin" / "dev")
        command = [sys.executable, "-I", "-S", script, "jobs", "exec", "gc"]
        child = [script, "gc", "--apply", "--yes", "--auto-only"]
        env = {"DEVKIT_AGENT": "job", "DEVKIT_JOB": "gc"}
        valid = {os.getppid(): (1, shlex.join(command)), 1: (0, "/sbin/launchd")}
        service = '{\n\t"Label" = "com.devkit.job.gc";\n\t"PID" = ' + str(os.getppid()) + ';\n};\n'
        cases = [
            ({}, child, env),
            ({os.getppid(): valid[os.getppid()]}, child, env),
            ({os.getppid(): (1, "'malformed"), 1: valid[1]}, child, env),
            ({os.getppid(): (1, shlex.join(command[:3] + ["/other/bin/dev"] + command[4:])), 1: valid[1]}, child, env),
            ({os.getppid(): (1, shlex.join(command[:-1] + ["sleep"])), 1: valid[1]}, child, env),
            ({os.getppid(): (123, shlex.join(command)), 123: (1, "/bin/zsh"), 1: valid[1]}, child, env),
            ({os.getppid(): (123, shlex.join(command)), 123: (1, "codex"), 1: valid[1]}, child, env),
            (valid, child, dict(env, CODEX_THREAD_ID="unregistered")),
            (valid, child, dict(env, DEVKIT_FROM_AGENT="neo")),
            (valid, child, dict(env, DEVKIT_JOB="sleep")),
            (valid, child[:-1], env),
            (valid, child + ["--verbose"], env),
        ]
        with mock.patch.object(core, "set_by_developer", return_value=False), \
                mock.patch.object(core, "sh", return_value=(0, service, "")), \
                mock.patch.object(pg, "current_session", return_value=("cli", "")):
            for snapshot, argv, environment in cases:
                with self.subTest(snapshot=snapshot, argv=argv, environment=environment), \
                        mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(pg, "_ps", return_value=snapshot):
                    self.assertEqual(self.cleanup_command(), (2, 0))

    def test_cleanup_terminal_invalid_configuration_still_denies(self):
        with mock.patch.object(core, "set_by_developer", return_value=True), \
                mock.patch.object(core, "config_snapshot", return_value=({}, ["work.mode invalid"])):
            self.assertEqual(self.cleanup_command(), (2, 0))

    def test_live_claim_remains_owned_in_auto_without_human_prompt(self):
        from devkit import peers
        text = peers.refusal({"agent": "claude", "session": "owner", "resource": "branch:acme/api:fix/x"})
        self.assertIn("live owner's claim intact", text)
        self.assertNotIn("grant claim-break", text)
        self.assertIsNone(pg.use_grant("claude", "s1", action="claim-break", target="branch:acme/api:fix/x",
                                       repo="acme/api"))

    def test_auto_decision_records_policy_without_developer_answer(self):
        pg.start("Ship", plan=["build"])
        decision = pg.crew_decision("Release now?", options="A: yes | B: no", recommend="A",
                                    irreversible="release", target="v26.11.4")
        self.assertEqual((decision["state"], decision["by"]), ("decided", "auto"))
        self.assertEqual(decision["authorization"]["source"], "policy-auto")
        self.assertNotIn("human", json.loads(self.state.sess.read_text()))

    def test_assign_auto_delegation_records_policy_scope(self):
        pg.start("Ship", plan=["build"])
        run = pg.assign("build", "Neo: build", allow="push", scope="lib/**", activity="debug")
        step = run["plan"][0]
        self.assertEqual(step["allow"], ["push"])
        self.assertEqual(step["allow_evidence"]["push"]["source"], "policy-auto")

    def test_global_api_write_cannot_borrow_current_repository(self):
        action = {"action": "api-write", "target": "api/user/keys", "repo": "acme/api", "tier": 2,
                  "words": "gh api user/keys -X POST"}
        self.assertIsNotNone(guard._main_gate([action], "claude", "s1", None, None))

    def test_cross_project_main_operation_needs_the_typed_grant(self):
        action = {"action": "merge", "target": "12", "repo": "acme/web", "tier": 2,
                  "words": "dev pr merge 12"}
        denied = guard._main_gate([action], "claude", "s1", None, None)
        self.assertIsNotNone(denied)
        self.assertIn("grant merge 12 --repo acme/web", denied[1])
        decision = pg.authorization_decision("claude", "s1", action="merge", target="12", repo="acme/web",
                                              classified_repo=True)
        self.assertEqual(decision["source"], "policy-auto")
        self.assertEqual(pg.authorization_decision("claude", "s1", action="merge", target="12", repo="acme/web")
                         ["outcome"], "deny")

    def test_remote_account_switch_changes_receipt(self):
        with mock.patch.object(core, "sh", side_effect=[(0, '{"login":"alice","id":123}', ""),
                                                        (0, '{"login":"bob","id":456}', "")]):
            before = self.use(consume=False)
            after = self.use()
        self.assertNotEqual(before["account"], after["account"])
        self.assertNotEqual(before["policy_digest"], after["policy_digest"])

    def test_unresolved_remote_account_denies_automatic_write(self):
        self.assertEqual(self.use(consume=False)["source"], "policy-auto")
        with mock.patch.object(core, "sh", return_value=(1, "", "not logged in")):
            self.assertIsNone(self.use())

    def test_compound_resolves_remote_identity_once(self):
        actions = [{"action": "release", "target": tag, "repo": "acme/api", "tier": 2,
                    "words": "dev release --version " + tag} for tag in ("v26.11.4", "v26.11.5")]
        with mock.patch.object(core, "sh", return_value=(0, '{"login":"alice","id":123}', "")) as network:
            self.assertIsNone(guard._main_gate(actions, "claude", "s1", None, None))
        self.assertEqual(network.call_count, 1)

    def test_git_transport_binds_remote_without_provider_login(self):
        with mock.patch.object(core, "gitout", return_value="git@github.com:acme/api.git"), \
                mock.patch.object(core, "sh", side_effect=AssertionError("Git must not query provider login")):
            actions = guard._found([("push", "feat/example", "acme/api")], ["git", "push", "origin", "feat/example"],
                                    "/repo")
            self.assertEqual(actions[0]["git_transport"]["remote"], "origin")
            self.assertIsNone(guard._main_gate(actions, "claude", "s1", None, None))

    def test_supported_push_rejects_unresolved_remote(self):
        with mock.patch.object(core, "gitout", return_value=""):
            actions = guard._found([("push", "feat/example", "acme/api")], ["dev", "push"], "/repo")
            self.assertIsNotNone(guard._main_gate(actions, "claude", "s1", None, None))

    def test_supported_release_binds_git_transport_and_provider(self):
        with mock.patch.object(core, "gitout", return_value="git@github.com:other/private.git"):
            actions = guard._found([("release", "v26.11.4", "acme/api")],
                                    ["dev", "release", "--version", "26.11.4"], "/repo")
            self.assertIsNotNone(guard._main_gate(actions, "claude", "s1", None, None))

    def test_git_remote_switch_is_rejected_at_consumption(self):
        with mock.patch.object(core, "gitout", side_effect=["git@github.com:acme/api.git", "",
                                                           "git@github.com:other/private.git", ""]):
            facts = pg.git_transport_account("acme/api", "/repo", "origin")
            denied = pg.use_grant("claude", "s1", action="push", target="feat/example", repo="acme/api",
                                  account_facts=facts)
        self.assertIsNone(denied)

    def test_backup_ask_uses_policy_without_input_and_preserves_off(self):
        from types import SimpleNamespace
        from devkit import backup
        context = {"vis": {"origin": "private"}, "policy": {"policy": "ask"}, "public_ok": False,
                   "auto": False, "dry_run": False, "asked": False}
        project = SimpleNamespace(pid="acme/api", top="/repo")
        # an agent caller: a developer at a terminal keeps the backup question in auto mode
        with mock.patch.object(backup, "_public"), \
                mock.patch.object(core, "set_by_developer", return_value=False), \
                mock.patch.object(core, "gitout", return_value="git@github.com:acme/api.git"), \
                mock.patch("builtins.input", side_effect=AssertionError("auto must not prompt")):
            self.assertIsNone(backup._policy_gate(project, "feat/example", "origin", context))
            # a public repository is the developer's: an agent needs their typed grant
            context["vis"] = {"origin": "public"}
            refused = backup._policy_gate(project, "feat/example", "origin", context)
            self.assertIsNotNone(refused)
            self.assertTrue(any("grant backup acme/api" in fix for fix in refused["fix"]), refused)
            context["vis"] = {"origin": "private"}
            context["policy"] = {"policy": "off"}
            self.assertIsNotNone(backup._policy_gate(project, "feat/example", "origin", context))

    def test_supported_main_install_uses_policy_but_member_remains_denied(self):
        for payload, allowed in (({"session_id": "s1"}, True),
                                 ({"session_id": "s1", "agent_id": "member"}, False)):
            token = guard._CALLER.set(guard._Caller("claude", "/repo", payload))
            try:
                verdict = guard._Shell("dev install", "/repo").dev(["install"], "/repo")
                self.assertEqual(verdict is None, allowed)
            finally:
                guard._CALLER.reset(token)

    def test_supported_main_configuration_edit_uses_policy(self):
        token = guard._CALLER.set(guard._Caller("claude", "/repo", {"session_id": "s1"}))
        try:
            self.assertIsNone(guard._Shell("dev config edit", "/repo").dev(["config", "edit"], "/repo"))
        finally:
            guard._CALLER.reset(token)

    def test_member_policy_requires_current_delegated_step(self):
        from tests.test_progress import launch
        pg.start("Ship", plan=["build"])
        pg.assign("build", "Neo: build", allow="push", scope="lib/**", activity="debug")
        launch("worker", "general-purpose", "Neo: build")
        first = pg.use_grant("claude", "s1", action="push", target="feat/example", repo="acme/api", lane="worker")
        self.assertIsNotNone(first)
        self.assertEqual(first["source"], "policy-auto")
        self.assertIsNone(pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo="acme/api",
                                       lane="worker"))
        run = pg.load_run("claude", "s1")
        run["plan"][0]["owner"] = "main"
        pg._save(run)
        self.assertIsNone(pg.use_grant("claude", "s1", action="push", target="feat/example", repo="acme/api",
                                       lane="worker"))

    def test_guided_integrity_denial_does_not_accept_headless_override(self):
        self.config["work"]["mode"] = "guided"
        self.state.sess.unlink()
        with mock.patch.object(pg, "headless", return_value=True), mock.patch.object(pg, "preapproved", return_value=True):
            self.assertIsNone(self.use())

    def test_policy_receipt_does_not_replace_release_ci_evidence(self):
        from tests.test_gitx import Sandbox, dev, YY, MM
        with Sandbox() as sandbox:
            version = f"{YY}.{MM}.0"
            session = json.loads(self.state.sess.read_text())
            session["project"] = "acme/widget"
            self.state.sess.write_text(json.dumps(session))
            receipt = pg.use_grant("claude", "s1", action="release", target="v" + version, repo="acme/widget")
            self.assertEqual(receipt["source"], "policy-auto")
            rc, output, error = dev("release", "--version", version)
            self.assertNotEqual(rc, 0)
            self.assertIn("no local CI evidence", output + error)
            self.assertFalse(sandbox.gh.called("release", "create"))



class AutoGateFallbackTests(unittest.TestCase):
    """The main session's gate in automatic mode: policy first, then the developer's typed grants."""

    def setUp(self):
        from tests.test_evidence import EvidenceCase
        self.fixture = EvidenceCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = {"work": {"mode": "auto"}}
        self.real_sh = core.sh
        for patch in (mock.patch.object(core, "config_snapshot", create=True,
                                        side_effect=lambda: (self.config, [])),
                      mock.patch.object(core, "sh", side_effect=self.sh),
                      # the audit week must never stand in for automatic policy or a grant
                      mock.patch.object(guard, "irreversible_setting", return_value="audit")):
            patch.start()
            self.addCleanup(patch.stop)

    def sh(self, argv, **kw):
        if argv[0] == "gh":
            return 0, '{"login":"alice","id":123}', ""
        return self.real_sh(argv, **kw)

    def records(self):
        files = sorted((self.root / "audit").glob("*.jsonl"))
        return [r for f in files for r in map(json.loads, f.read_text().splitlines())
                if r["action"] == "guard.irreversible"]

    def gate(self, acts, now=None):
        from tests.test_evidence import m
        return guard._main_gate(acts, "claude", "s1", None, now or m(2))

    @staticmethod
    def unknown(repo="acme/api", why=("built", "getattr(module, name)")):
        return {"action": "unknown", "tier": 2, "target": "cmd-0123456789", "repo": repo,
                "words": "python3 tools/build.py", **({"why": why} if why else {})}

    def test_unknown_program_runs_with_an_audit_record(self):
        for why in (("built", "getattr(module, name)"), ("unsure", "a GraphQL query from stdin"),
                    ("unread", "/repo/tools/huge.py"), None):
            with self.subTest(why=why):
                self.assertIsNone(self.gate([self.unknown(why=why)]))
                record = self.records()[-1]
                self.assertEqual((record["target"], record["outcome"]), ("cmd-0123456789", "ok"))
                self.assertEqual((record["ref"]["source"], record["ref"]["irreversible"]),
                                 ("policy-auto", "unknown"))
                self.assertTrue(record["ref"]["grant"].startswith("policy-auto:"))
                self.assertEqual(record["ref"]["kind"], "unclassified:" + (why[0] if why else "opaque"))
                self.assertEqual(record["evidence"]["match"],
                                 why[1] if why else "the guard cannot see which program runs")

    def test_guided_typed_grant_runs_an_unknown_program_once(self):
        from tests.test_evidence import m
        self.config["work"]["mode"] = "guided"
        with mock.patch.object(guard, "irreversible_setting", return_value="enforce"):
            self.assertIsNotNone(self.gate([self.unknown()]))
            self.fixture.typed("grant unknown cmd-0123456789", m(3))
            self.assertIsNone(self.gate([self.unknown()], m(4)))
            self.assertEqual((self.records()[-1]["ref"]["result"], self.records()[-1]["ref"]["source"]),
                             ("granted", "E2"))
            self.assertIsNotNone(self.gate([self.unknown()], m(5)), "a typed grant is used once")

    def test_a_member_still_routes_an_unknown_program_to_the_tech_lead(self):
        from tests.test_evidence import m
        cmd = "$(echo gh) pr merge 12"
        self.assertEqual([a["action"] for a in guard.irreversible_actions(cmd, str(self.root))], ["unknown"])
        why = guard._member_gate(cmd, str(self.root), "tank", "a_tank", "claude", "s1", None, m(2))
        self.assertIsNotNone(why)
        self.assertIn("tech lead", why)
        self.assertEqual(self.records()[-1]["ref"]["result"], "deny")

    def test_unknown_program_outside_a_repository_uses_the_session_project(self):
        self.assertIsNone(self.gate([self.unknown(repo="")]))
        self.assertEqual(self.records()[-1]["ref"]["source"], "policy-auto")

    def test_unknown_program_stays_refused_for_an_unresolved_caller(self):
        session = json.loads(self.fixture.state.sess.read_text())
        self.fixture.state.sess.write_text(json.dumps(dict(session, ended=True)))
        denied = self.gate([self.unknown()])
        self.assertIsNotNone(denied)
        self.assertIn("caller", denied[1])
        self.assertEqual(self.records()[-1]["ref"]["result"], "deny")

    def test_unknown_program_stays_refused_with_invalid_configuration(self):
        with mock.patch.object(core, "config_snapshot", return_value=({}, ["work.mode: invalid"])):
            denied = self.gate([self.unknown()])
        self.assertIsNotNone(denied)
        self.assertIn("Invalid policy configuration", denied[1])

    def test_developer_only_decision_needs_the_typed_grant(self):
        from tests.test_evidence import m
        lift = {"action": "developer-only", "tier": 2, "target": "embargo/feat/x", "repo": "acme/api",
                "words": "dev branch embargo feat/x --lift"}
        denied = self.gate([lift])
        self.assertIsNotNone(denied)
        self.assertIn("grant embargo feat/x", denied[1])
        self.fixture.typed("grant embargo feat/x", m(3))
        self.assertIsNone(self.gate([lift], m(4)))
        self.assertEqual((self.records()[-1]["ref"]["result"], self.records()[-1]["ref"]["source"]),
                         ("granted", "E2"))
        self.assertIsNotNone(self.gate([lift], m(5)), "a typed grant is used once")

    def test_policy_problem_needs_the_typed_grant(self):
        from tests.test_evidence import m
        push = {"action": "push", "tier": 1, "target": "feat/x", "repo": "acme/api",
                "words": "git -c core.sshCommand=x push origin feat/x",
                "policy_problem": "Command-local Git configuration cannot be attested."}
        denied = self.gate([push])
        self.assertIsNotNone(denied)
        self.assertIn("cannot be attested", denied[1])
        self.assertIn("grant push feat/x", denied[1])
        self.fixture.typed("grant push feat/x", m(3))
        self.assertIsNone(self.gate([push], m(4)))

    def test_another_repository_falls_back_to_the_typed_grant(self):
        from tests.test_evidence import m
        release = {"action": "release", "tier": 2, "target": "v1.2.0", "repo": "acme/other",
                   "words": "gh release create v1.2.0 -R acme/other"}
        denied = self.gate([release])
        self.assertIsNotNone(denied)
        self.assertIn("grant release v1.2.0 --repo acme/other", denied[1])
        self.fixture.typed("grant release v1.2.0 --repo acme/other", m(3))
        self.assertIsNone(self.gate([release], m(4)))
        record = self.records()[-1]
        self.assertEqual((record["ref"]["result"], record["ref"]["source"]), ("granted", "E2"))
        self.assertIsNotNone(self.gate([release], m(5)), "a typed grant is used once")

    def test_mcp_write_falls_back_to_the_typed_grant(self):
        from tests.test_evidence import m
        tool = "mcp__slack__send_message"
        denied = guard.irreversible_tool_verdict(tool, {"text": "hi"}, str(self.root), agent="claude",
                                                 session="s1", now=m(2))
        self.assertIsNotNone(denied)
        self.assertIn("grant external-message " + tool, denied[1])
        self.fixture.typed("grant external-message " + tool, m(3))
        with mock.patch.object(guard, "_repo_of", return_value="acme/api"):
            self.assertIsNone(guard.irreversible_tool_verdict(tool, {"text": "hi"}, str(self.root),
                                                              agent="claude", session="s1", now=m(4)))


class AutoGateCommandTests(unittest.TestCase):
    """Routine delivery commands of the main session, judged end to end in automatic mode."""

    COMMANDS = ("npm run release", "dev release --version 26.11.5", "gh pr create --fill", "dev pr create",
                "git push origin feat/x", "dev pr merge 12")

    def setUp(self):
        from tests.test_evidence import EvidenceCase
        self.fixture = EvidenceCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = {"work": {"mode": "auto"}}
        self.real_sh = core.sh
        self.project = "acme/api"
        # an empty handoff of the test's own: no embargo anywhere (an unreadable note holds every push)
        (self.root / "handoff" / "projects").mkdir(parents=True)
        for patch in (mock.patch.object(core, "config_snapshot", create=True,
                                        side_effect=lambda: (self.config, [])),
                      mock.patch.object(core, "HANDOFF", self.root / "handoff"),
                      mock.patch.object(core, "NOTES", self.root / "handoff" / "projects"),
                      mock.patch.object(core, "sh", side_effect=self.sh),
                      mock.patch.object(guard, "current_project", side_effect=lambda cwd: self.project),
                      mock.patch.object(guard, "irreversible_setting", return_value="audit")):
            patch.start()
            self.addCleanup(patch.stop)

    def sh(self, argv, **kw):
        if argv[0] == "gh":
            return 0, '{"login":"alice","id":123}', ""
        return self.real_sh(argv, **kw)

    def repo(self, name):
        repo = self.root / name
        repo.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                           check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (repo / "package.json").write_text(json.dumps({"name": "widget", "version": "1.4.0",
                                                       "scripts": {"release": "node release.js"}}))
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        git("remote", "add", "origin", f"https://github.com/{self.project}.git")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git("checkout", "-q", "-b", "feat/x")
        return str(repo)

    def verdict(self, cmd, cwd, now):
        return guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", now=now)

    def test_routine_delivery_in_the_own_repository_needs_no_grant(self):
        from tests.test_evidence import m
        cwd = self.repo("api")
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                acts = guard.irreversible_actions(cmd, cwd)
                self.assertTrue(acts, "the command is classified")
                self.assertIsNone(self.verdict(cmd, cwd, m(2)), [{k: a.get(k) for k in ("action", "target", "repo", "why")} for a in acts])

    def test_routine_delivery_in_another_repository_needs_a_typed_grant(self):
        from tests.test_evidence import m
        self.project = "acme/other"
        cwd = self.repo("other")
        for k, cmd in enumerate(self.COMMANDS):
            with self.subTest(cmd=cmd):
                acts = guard.irreversible_actions(cmd, cwd)
                self.assertEqual(len(acts), 1, acts)
                line = f"grant {acts[0]['action']} {acts[0]['target']} --repo acme/other"
                denied = self.verdict(cmd, cwd, m(2 + k))
                self.assertIsNotNone(denied)
                self.assertIn(line, denied[1])
                self.fixture.typed(line, m(2 + k))
                self.assertIsNone(self.verdict(cmd, cwd, m(2 + k)))
