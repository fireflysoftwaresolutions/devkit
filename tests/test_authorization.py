# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Behavior of the pure authorization boundary, without state or grant fixtures."""
import copy
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class AuthorizationTests(unittest.TestCase):
    def request(self, action="release", tier=2):
        return {
            "mode": "auto", "mode_source": "config", "action": action,
            "target": "v26.11.19", "repo": "acme/api",
            "caller": {"session": "codex:session-one", "kind": "main",
                       "role": "morpheus", "authenticated": True, "lane": None},
            "context": {
                "config_valid": True, "registry": {"action": action, "tier": tier},
                "scope": {"allowed": True, "repo": "acme/api", "target": "v26.11.19",
                          "account": "github:acme"},
                "readiness": {"allowed": True}, "prohibitions": [],
                "account": "github:acme", "account_resolved": True,
            },
        }

    def decide(self, request):
        self.assertIsNotNone(importlib.util.find_spec("devkit.authorization"),
                             "The pure authorization decision API is missing")
        return importlib.import_module("devkit.authorization").decide(**request)

    def assert_denied(self, request, reason):
        result = self.decide(request)
        self.assertEqual((result["outcome"], result["source"]), ("deny", "integrity"))
        self.assertIn(reason, result["reason"])
        return result

    def test_main_auto_authorizes_recognized_actions_without_human_evidence(self):
        for action, tier in (("push", 1), ("pr", 1), ("release", 2), ("merge", 2),
                             ("publish", 2), ("deploy", 2), ("api-write", 2),
                             ("external-message", 2), ("config-set", 2), ("install", 2)):
            with self.subTest(action=action):
                request = self.request(action, tier)
                result = self.decide(request)
                self.assertEqual((result["outcome"], result["source"]), ("allow", "policy-auto"))
                self.assertEqual((result["action"], result["target"], result["repo"]),
                                 (action, "v26.11.19", "acme/api"))
                self.assertEqual(set(result), {"outcome", "source", "reason", "action", "target",
                                               "repo", "policy_digest"})

    def test_guided_requires_human_after_integrity_checks(self):
        request = self.request()
        request["mode"] = "guided"
        result = self.decide(request)
        self.assertEqual((result["outcome"], result["source"]), ("require-human", "guided"))
        request["context"]["readiness"]["allowed"] = False
        self.assert_denied(request, "readiness")

    def test_invalid_mode_or_origin_cannot_fall_back_to_auto(self):
        for field, values in (("mode", (None, "", "AUTO", "banana", True, [])),
                              ("mode_source", (None, "", "tool-input", True, {}))):
            for value in values:
                with self.subTest(field=field, value=value):
                    request = self.request()
                    request[field] = value
                    self.assert_denied(request, "mode")

    def test_invalid_configuration_blocks_both_modes(self):
        for mode in ("auto", "guided"):
            for value in (False, None, "true", 1):
                with self.subTest(mode=mode, value=value):
                    request = self.request()
                    request["mode"] = mode
                    request["context"]["config_valid"] = value
                    self.assert_denied(request, "configuration")

    def test_developer_decisions_and_ineligible_operations_need_human_authority(self):
        for action in ("developer-only", "claim-break"):
            with self.subTest(action=action):
                result = self.decide(self.request(action))
                self.assertEqual((result["outcome"], result["source"]), ("require-human", "human-only"))
        for value in (False, None, 1, "true"):
            with self.subTest(policy_eligible=value):
                request = self.request()
                request["context"]["policy_eligible"] = value
                result = self.decide(request)
                self.assertEqual((result["outcome"], result["source"]), ("require-human", "human-only"))
        request = self.request()
        request["context"]["policy_eligible"] = False
        request["context"]["prohibitions"] = ["developer-refused"]
        self.assert_denied(request, "prohibition")

    def unknown(self, mode="auto", target="cmd-0123456789"):
        request = self.request("unknown")
        request.update(mode=mode, target=target)
        request["context"]["scope"]["target"] = target
        return request

    def test_unknown_program_is_policy_for_the_main_session_only(self):
        result = self.decide(self.unknown())
        self.assertEqual((result["outcome"], result["source"]), ("allow", "policy-auto"))
        self.assertEqual(self.decide(self.unknown("guided"))["outcome"], "require-human")
        request = self.unknown()
        request["context"]["registry"] = None  # the classifier's verdict carries no catalog entry
        self.assert_denied(request, "operation")
        member = self.unknown()
        member["caller"].update(kind="member", lane="tank-1", step="api", live_step=True,
                                delegated_actions=["push"])
        self.assert_denied(member, "tech lead")
        refused = self.unknown()
        refused["context"]["prohibitions"] = ["developer-refused"]
        self.assert_denied(refused, "prohibition")

    def test_unknown_program_needs_its_exact_command_digest(self):
        for target in ("dynamic", "cmd-", "cmd-0123", "cmd-0123456789a", "cmd-ABCDEF0123", "cmd-*"):
            with self.subTest(target=target):
                self.assert_denied(self.unknown(target=target), "operation")

    def test_unknown_and_prohibited_operation_classes_never_authorize(self):
        # unknown is denied here because its target is no command digest (see the unknown tests above)
        for action in ("unknown", "unparsed", "registry-write"):
            with self.subTest(action=action):
                self.assert_denied(self.request(action), "operation")
        for registry in (None, {}, {"action": "merge", "tier": 2},
                         {"action": "release", "tier": True}, {"action": "release", "tier": 0}):
            with self.subTest(registry=registry):
                request = self.request()
                request["context"]["registry"] = registry
                self.assert_denied(request, "operation")

    def test_unresolved_operation_and_repository_are_refused(self):
        for field, values in (("action", ("", None, " release", "new action")),
                              ("target", ("", None, "?", "*", "refs/heads/*", "${BRANCH}", "release/{}")),
                              ("repo", ("", None, "acme", "../api", "acme/*", "acme/api "))):
            for value in values:
                with self.subTest(field=field, value=value):
                    request = self.request()
                    request[field] = value
                    if field in ("target", "repo"):
                        request["context"]["scope"][field] = value
                    else:
                        request["context"]["registry"]["action"] = value
                    self.assert_denied(request, "operation")

    def test_authenticated_current_caller_is_required(self):
        for field, values in (("authenticated", (False, None, 1, "true")),
                              ("session", (None, "", "?")), ("role", (None, "")),
                              ("kind", (None, "", "unregistered")), ("lane", ("member-lane",))):
            for value in values:
                with self.subTest(field=field, value=value):
                    request = self.request()
                    request["caller"][field] = value
                    self.assert_denied(request, "caller")

    def test_remote_account_must_be_resolved_and_match_scope(self):
        for values in ({"account": ""}, {"account": "?"}, {"account_resolved": False},
                       {"account_resolved": 1}, {"account": "github:other"}):
            with self.subTest(values=values):
                request = self.request()
                request["context"].update(values)
                self.assert_denied(request, "account")

    def test_scope_must_allow_exact_repository_and_resource(self):
        for values in ({"allowed": False}, {"allowed": 1}, {"repo": "acme/other"},
                       {"target": "v26.11.20"}, {"repo": "acme/*"}, {"target": "*"}):
            with self.subTest(values=values):
                request = self.request()
                request["context"]["scope"].update(values)
                self.assert_denied(request, "scope")

    def test_explicit_off_and_embargo_precede_auto_and_do_not_leak_reasons(self):
        for mode in ("auto", "guided"):
            for prohibition in ("backup-off", "embargo", "credential:private-token"):
                with self.subTest(mode=mode, prohibition=prohibition):
                    request = self.request()
                    request["mode"] = mode
                    request["context"]["prohibitions"] = [prohibition]
                    result = self.assert_denied(request, "prohibition")
                    self.assertNotIn(prohibition, result["reason"])

    def test_readiness_never_accepts_truthy_or_absent_verdicts(self):
        for readiness in ({}, None, {"allowed": False}, {"allowed": 1}, {"allowed": "true"}):
            with self.subTest(readiness=readiness):
                request = self.request()
                request["context"]["readiness"] = readiness
                self.assert_denied(request, "readiness")

    def test_missing_authority_fields_deny_instead_of_defaulting(self):
        for field in self.request()["context"]:
            with self.subTest(field=field):
                request = self.request()
                del request["context"][field]
                self.assertEqual(self.decide(request)["outcome"], "deny")

    def member(self, action="push", tier=1):
        request = self.request(action, tier)
        request["caller"].update(kind="member", role="tank", lane="lane-one",
                                  step="api", live_step=True, delegated_actions=[action])
        return request

    def test_member_auto_requires_live_delegation_with_role_and_scope(self):
        request = self.member()
        self.assertEqual(self.decide(request)["outcome"], "allow")
        for field, value in (("lane", None), ("step", ""), ("live_step", False),
                             ("live_step", 1), ("delegated_actions", []),
                             ("delegated_actions", ["pr"]), ("delegated_actions", "push")):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(request)
                changed["caller"][field] = value
                self.assert_denied(changed, "member")
        request["context"]["scope"]["allowed"] = False
        self.assert_denied(request, "scope")

    def test_member_tier_two_routes_to_lead_in_both_modes(self):
        for mode in ("auto", "guided"):
            request = self.member("release", 2)
            request["mode"] = mode
            self.assert_denied(request, "tech lead")
        request = self.member("release", 1)
        self.assert_denied(request, "tech lead")

    def test_member_guided_needs_human_even_with_valid_delegation(self):
        request = self.member()
        request["mode"] = "guided"
        result = self.decide(request)
        self.assertEqual((result["outcome"], result["source"]), ("require-human", "guided"))

    def test_digest_is_canonical_and_decisions_do_not_mutate_inputs(self):
        request = self.member()
        before = copy.deepcopy(request)
        result = self.decide(request)
        reordered = dict(reversed(list(request.items())))
        reordered["context"] = dict(reversed(list(request["context"].items())))
        self.assertEqual(result, self.decide(reordered))
        self.assertEqual(request, before)
        self.assertRegex(result["policy_digest"], r"^[a-f0-9]{64}$")

    def test_digest_binds_mode_origin_operation_identity_scope_and_readiness(self):
        request = self.request()
        digest = self.decide(request)["policy_digest"]
        for path, value in ((("mode",), "guided"), (("mode_source",), "run"),
                            (("action",), "merge"), (("target",), "v26.11.20"),
                            (("repo",), "acme/other"), (("caller", "session"), "codex:other"),
                            (("caller", "role"), "neo"), (("context", "account"), "github:other"),
                            (("context", "scope", "allowed"), False),
                            (("context", "readiness", "allowed"), False),
                            (("context", "prohibitions"), ["embargo"])):
            with self.subTest(path=path):
                changed = copy.deepcopy(request)
                node = changed
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                self.assertNotEqual(self.decide(changed)["policy_digest"], digest)

    def test_malformed_non_json_context_is_denied_without_exception(self):
        for value in (None, [], "trusted", {"extra": float("nan")}, {"extra": object()}):
            with self.subTest(value=type(value).__name__):
                request = self.request()
                request["context"] = value
                self.assertEqual(self.decide(request)["outcome"], "deny")


if __name__ == "__main__":
    unittest.main()
