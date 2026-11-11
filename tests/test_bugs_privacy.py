# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a bug report may carry: an allowlist of devkit facts, never a copy of something with parts removed. Free
text is templated or scrubbed against this machine's private words and always held for the developer's review, and
anything that touches the guard goes to private reporting only."""
import io
import json
import random
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import bugs, core  # noqa: E402

DENY = frozenset({"acme", "payments-core", "invoice-export", "feat/invoice-export", "ana", "mbp-ana", "ledgerly",
                  "billing-revamp", "zeta-internal"})


class ReportTests(unittest.TestCase):
    def test_report_has_only_allowlisted_keys(self):
        occ = {"ts": 1.0, "kind": "crash", "fingerprint": "3f9a1c2e7b10", "code": "k7q2m9", "impact": "blocked",
               "session": "s1", "version": "26.11.7", "command": "progress step", "exc_class": "KeyError",
               "frames": ["work.actor_str", "<local>"], "word": "./deploy-acme-prod.sh", "event": "post-tool",
               "exit": 70, "duration": 3.2, "agent": "claude", "surface": "terminal",
               "cwd": "/Users/ana/acme/payments-core", "args": ["--branch", "feat/invoice-export"],
               "message": "no such file: /Users/ana/acme/payments-core/x.py"}
        report = bugs.build_report(occ)
        self.assertEqual(set(report), {"version", "channel", "macos", "python", "agent", "surface", "command",
                                       "exc_class", "frames", "refusal_id", "word", "event", "exit", "duration",
                                       "fingerprint", "impact", "kind"})
        self.assertEqual((report["command"], report["word"], report["frames"], report["duration"]),
                         ("progress step", "<path>", ["work.actor_str", "<local>"], "1-10s"))
        self.assertNotIn("acme", json.dumps(report))
        self.assertNotIn("ana", json.dumps(report))

    def test_report_keeps_no_value_outside_its_vocabulary(self):
        occ = {"kind": "crash", "fingerprint": "3f9a1c2e7b10", "command": "deploy acme", "exc_class": "acme.Err(x)",
               "frames": ["payments-core/x.py:12"], "agent": "acme-bot", "surface": "/Users/ana", "event": "acme hook",
               "refusal_id": "rm -rf /Users/ana", "impact": "catastrophic", "version": "/Users/ana"}
        report = bugs.build_report(occ)
        self.assertEqual((report["command"], report["exc_class"], report["frames"], report["agent"],
                          report["surface"], report["event"], report["refusal_id"], report["impact"]),
                         ("", "", ["<local>"], "", "", "", "", "blocked"))
        self.assertEqual(report["version"], core.VERSION)
        self.assertNotIn("acme", json.dumps(report))

    def test_public_word(self):
        self.assertEqual(bugs.public_word("sed"), "sed")
        self.assertEqual(bugs.public_word("git"), "git")
        self.assertEqual(bugs.public_word("./deploy-acme-prod.sh"), "<path>")
        self.assertEqual(bugs.public_word("~/bin/tool"), "<path>")
        self.assertEqual(bugs.public_word("/usr/bin/sed"), "<path>")
        self.assertEqual(bugs.public_word("acme-cli"), "<program>")

    def test_public_word_comes_from_the_guards_own_list(self):
        from devkit import guard
        bugs._public_words.cache_clear()
        self.addCleanup(bugs._public_words.cache_clear)
        listed = frozenset({"zprog", "bad word", "-rf", "--force", "FOO=bar", "x=1"})
        with mock.patch.object(guard, "COMMAND_WORDS", listed, create=True):
            bugs._public_words.cache_clear()
            self.assertEqual(bugs.public_word("zprog"), "zprog")
            self.assertEqual(bugs.public_word("sed"), "<program>")  # not on the guard's list: not public
            for word in ("bad word", "-rf", "--force", "FOO=bar", "x=1"):  # an option or assignment is no program
                with self.subTest(word=word):
                    self.assertEqual(bugs.public_word(word), "<program>")


GUARD_REPORT = {"devkit_bug": 1, "type": "report", "fingerprint": "cccccccccccc", "version": "26.11.7",
                "impact": "blocked", "kind": "guard", "word": ""}
CRASH_REPORT = {"devkit_bug": 1, "type": "report", "fingerprint": "cccccccccccc", "version": "26.11.7",
                "impact": "blocked", "kind": "crash", "command": "progress step", "exc_class": "KeyError",
                "frames": ["work.actor_str"]}
TOKEN = "ghp_" + "A1b2C3d4E5f6" * 3  # the shape of a GitHub token, built here so no literal looks like one


def refused(payload, field):
    return f"report field {field} has a value it does not allow" in bugs.validate_payload(payload)


class FieldVocabularyTests(unittest.TestCase):
    def test_a_refusal_id_is_one_the_guard_or_its_hook_makes(self):
        ids = bugs.guard_refusal_ids()
        for rid in ("check_git:force-push", "check_rm:git-dir", "steer_rm", "hooks:crew-scope", "hooks:irreversible"):
            self.assertIn(rid, ids)
        for rid in ("acme:payments-core", "_check_sed", "check_git", "hooks:anything", "sk_live_x:y"):
            self.assertNotIn(rid, ids)

    def test_the_schema_and_the_report_take_only_the_guards_refusal_ids(self):
        # the workflow runs the same check, so an id a stranger made up never becomes an issue title
        self.assertEqual(bugs.validate_payload({**GUARD_REPORT, "refusal_id": "check_git:force-push"}), [])
        for rid in ("acme:ledgerly-export", "x:payments-core", "_check_sed"):
            with self.subTest(rid=rid):
                self.assertTrue(refused({**GUARD_REPORT, "refusal_id": rid}, "refusal_id"))
                self.assertEqual(bugs.build_report({"kind": "guard", "refusal_id": rid})["refusal_id"], "")
        self.assertEqual(bugs.build_report({"kind": "guard", "refusal_id": "check_rm:git-dir"})["refusal_id"],
                         "check_rm:git-dir")

    def test_every_refusal_the_guard_hook_names_is_listed(self):
        # the hook names its own refusals hooks:<rule>: a literal id, a judge's name, or the rule of a lane refusal
        import ast
        from devkit import hooks
        tree = ast.parse((Path(__file__).resolve().parents[1] / "lib" / "devkit" / "hooks.py").read_text())
        named = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("hooks:") \
                    and node.value != "hooks:":  # the prefix of f"hooks:{…}" names no rule
                named.add(node.value[len("hooks:"):])
            if isinstance(node, ast.FunctionDef) and node.name == "_lane_rule":
                named |= {r.value.value for r in ast.walk(node) if isinstance(r, ast.Return)
                          and isinstance(r.value, ast.Constant)}
            if isinstance(node, ast.FunctionDef) and node.name in ("_guard", "_guard_in_context"):
                for a in ast.walk(node):
                    if isinstance(a, ast.Assign) and isinstance(a.targets[0], ast.Tuple) and \
                            isinstance(a.value, ast.Tuple):
                        named |= {v.value for t, v in zip(a.targets[0].elts, a.value.elts) if isinstance(t, ast.Name)
                                  and t.id == "made" and isinstance(v, ast.Constant) and v.value}
                    if isinstance(a, ast.For) and isinstance(a.iter, ast.Tuple):
                        named |= {e.elts[0].value for e in a.iter.elts if isinstance(e, ast.Tuple)
                                  and isinstance(e.elts[0], ast.Constant)}
        self.assertGreaterEqual(len(named), 9)
        self.assertEqual(named, set(hooks.HOOK_REFUSALS))

    def test_an_exception_class_is_a_python_name_path_with_no_secret(self):
        for name in ("KeyError", "json.decoder.JSONDecodeError", "Q" * 80):
            with self.subTest(name=name):
                self.assertEqual(bugs.validate_payload({**CRASH_REPORT, "exc_class": name}), [])
        for name in ("Q" * 81, "Key Error", "acme/x.KeyError", ".KeyError", "a..b", "Err(x)", TOKEN,
                     "sk_live_" + "A1b2C3d4E5f6A1b2", "deadbeefdeadbeef01Error"):
            with self.subTest(name=name):
                self.assertTrue(refused({**CRASH_REPORT, "exc_class": name}, "exc_class"))
                self.assertEqual(bugs.build_report({"kind": "crash", "exc_class": name})["exc_class"], "")

    def test_frames_name_functions_of_this_devkit(self):
        self.assertEqual(bugs.validate_payload(
            {**CRASH_REPORT, "frames": ["work.actor_str", "<local>", "hooks.<lambda>", "guard.check_git"]}), [])
        for frames in (["acme_billing.charge_card"], ["work.no_such_function"], ["ledgerly_core.<lambda>"]):
            with self.subTest(frames=frames):
                self.assertTrue(refused({**CRASH_REPORT, "frames": frames}, "frames"))
        self.assertEqual(bugs.build_report({"kind": "crash", "frames": ["acme_billing.charge_card", "work.actor_str"]})
                         ["frames"], ["<local>", "work.actor_str"])


class TemplateTests(unittest.TestCase):
    def test_template_message_removes_values(self):
        t = bugs.template_message("no such file: '/Users/ana/acme/payments-core/x.py' (line 42)", deny=frozenset())
        self.assertEqual(t, "no such file: <str> (line <num>)")
        self.assertEqual(bugs.template_message("cannot read ~/acme/payments-core/x.py", deny=frozenset()),
                         "cannot read <path>")
        token = "ghp_" + "A1b2C3d4E5f6" * 3  # the shape of a GitHub token, built here so no literal looks like one
        t = bugs.template_message("push to https://git.acme.io/team/app failed for ana@acme.io after 3 tries "
                                  f"(token {token})", deny=frozenset())
        self.assertEqual(t, "push to <url> failed for <email> after <num> tries (token <hex>)")

    def test_a_template_that_still_names_a_private_word_is_dropped(self):
        self.assertEqual(bugs.template_message("KeyError: ledgerly not in the map", deny=DENY), "")
        self.assertEqual(bugs.template_message("KeyError: branch not in the map", deny=DENY),
                         "KeyError: branch not in the map")

    def test_scrub_never_leaks_generated_values(self):
        rng = random.Random(7)
        filler = ["failed", "while", "reading", "the", "config", "for", "branch", "in", "project", "at", "line",
                  "could", "not", "open", "after", "retry", "with", "value", "from", "step"]
        alnum = string.ascii_letters + string.digits

        def name():  # a random name that is on no deny list: only its own rule can remove the value it is in
            return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randrange(5, 11)))

        def sensitive():
            kind = rng.randrange(6)
            if kind == 0:
                return rng.choice(sorted(DENY))
            if kind == 1:
                return rng.choice(["/Users/", "/home/", "~/"]) + name() + "/" + name() + "/main.py"
            if kind == 2:
                return f"{name()}{rng.choice(['', '.', '_'])}{name()}@{name()}.example"
            if kind == 3:
                return f"https://{name()}.example.com/{name()}?id={rng.randrange(10 ** 6)}"
            if kind == 4:
                return rng.choice(["ghp_", "sk-", "xoxb-"]) + "".join(rng.choice(alnum) for _ in range(36))
            return "".join(rng.choice("0123456789abcdef") for _ in range(rng.randrange(16, 41)))

        for i in range(2000):
            values, parts = [], []
            for _ in range(rng.randrange(1, 4)):
                parts += rng.sample(filler, 2)
                values.append(sensitive())
                parts.append(rng.choice(["{}", "'{}'", "({})", "{}:"]).format(values[-1]))
            msg = " ".join(parts)
            scrubbed, held = bugs.scrub_text(msg, DENY)
            template = bugs.template_message(msg, deny=DENY)
            self.assertTrue(held)
            for v in values:
                with self.subTest(i=i, value=v):
                    self.assertNotIn(v.lower(), scrubbed.lower())
                    self.assertNotIn(v.lower(), template.lower())
                    for w in DENY:
                        self.assertNotIn(w, scrubbed.lower())
                        self.assertNotIn(w, template.lower())

    def test_a_private_word_is_found_in_any_case_spacing_or_look_alike_letters(self):
        # the red team's spellings: underscores, spaces and camelCase for hyphens, Cyrillic and fullwidth letters,
        # an accent, a zero-width space
        cases = (("cannot open project payments_core", "payments_core"),
                 ("branch Invoice Export has no upstream", "Invoice Export"),
                 ("PaymentsCore is not a scope", "PaymentsCore"),
                 ("no such project \u0430cme", "\u0430cme"),
                 ("project \uff21\uff23\uff2d\uff25 missing", "\uff21\uff23\uff2d\uff25"),
                 ("lock held by LEDGERLY", "LEDGERLY"),
                 ("branch zeta internal is gone", "zeta internal"),
                 ("owner \u00e1na refused", "\u00e1na"),
                 ("cannot read a\u200bcme now", "a\u200bcme"))
        for msg, word in cases:
            with self.subTest(msg=msg):
                scrubbed, held = bugs.scrub_text(msg, DENY)
                self.assertNotIn(word, scrubbed)
                self.assertIn("<private>", scrubbed)
                self.assertEqual(bugs.template_message(msg, deny=DENY), "")
        self.assertEqual(bugs.scrub_text("cannot open project payments_core", DENY)[0],
                         "cannot open project <private>")
        for msg in ("the payment score is low", "payments are core to it"):  # no private word, only its letters
            with self.subTest(msg=msg):
                self.assertEqual(bugs.scrub_text(msg, DENY)[0], msg)

    def test_free_text_is_always_held(self):
        self.assertEqual(bugs.scrub_text("Run the step from the repository root.", DENY),
                         ("Run the step from the repository root.", True))
        self.assertEqual(bugs.scrub_text("   ", DENY), ("", False))
        self.assertEqual(bugs.scrub_text("", DENY), ("", False))


class DenyListTests(unittest.TestCase):
    def test_the_deny_list_comes_from_this_machine(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d)
            (h / "projects" / "acme").mkdir(parents=True)
            (h / "projects" / "acme" / "payments-core.md").write_text("# note\n")
            (h / "projects" / "gitlab.zeta.io" / "ops").mkdir(parents=True)
            (h / "projects" / "gitlab.zeta.io" / "ops" / "ledgerly.md").write_text("# note\n")
            (h / "initiatives" / "billing-revamp").mkdir(parents=True)
            (h / "registry.json").write_text(json.dumps({"projects": [
                {"id": "acme/payments-core", "owner": "acme", "name": "payments-core",
                 "remote": "git@gitlab.zeta.io:ops/payments-core.git", "path": "/Users/ana/Development/acme/x",
                 "branches": [{"name": "feat/invoice-export"}, {"name": "main"}]},
                {"id": "fireflysoftwaresolutions/devkit", "owner": "fireflysoftwaresolutions", "name": "devkit",
                 "remote": "https://github.com/fireflysoftwaresolutions/devkit.git", "branches": []}]}))
            reg = json.loads((h / "registry.json").read_text())
            reg["projects"][0]["worktrees"] = [{"path": "/Users/ana/Development/.worktrees/acme/x/spike-quartz",
                                                "branch": "spike/quartz"}]
            (h / "registry.json").write_text(json.dumps(reg))
            with mock.patch.object(core, "HANDOFF", h), mock.patch("getpass.getuser", lambda: "ana"), \
                    mock.patch.object(bugs, "_host_names",
                                      lambda: ["mbp-ana.corp.zeta.io", "Ana's MacBook Pro", "Anas-MacBook-Pro"]), \
                    mock.patch.object(bugs, "_git_identity", lambda: ("Ana Pérez", "ana.perez@zeta.io")):
                deny = bugs.deny_words()
                self.assertEqual(bugs.private_words("Octo-Ana") - deny, {"octo-ana"})  # the login that posts
        for w in ("acme", "payments-core", "feat/invoice-export", "invoice-export", "ana", "mbp-ana",
                  "gitlab.zeta.io", "ops", "ledgerly", "billing-revamp", str(Path.home()).lower(), "spike-quartz",
                  "spike/quartz", "ana pérez", "pérez", "ana.perez@zeta.io", "ana.perez", "zeta.io",
                  "mbp-ana.corp.zeta.io", "corp", "ana's macbook pro", "anas-macbook-pro"):
            self.assertIn(w, deny)
        # public words are never private: devkit's own repository, the default branch, generic parts, short words
        for w in ("devkit", "fireflysoftwaresolutions", "main", "feat", "local", "github.com", "x", "macbook", "pro"):
            self.assertNotIn(w, deny)
        msg = "payments-core failed on feat/invoice-export for ana@acme.io (invoice-export, mbp-ana)"
        out, held = bugs.scrub_text(msg, deny)
        self.assertTrue(held)
        for leak in ("payments-core", "invoice-export", "ana@acme.io", "mbp-ana", "acme"):
            self.assertNotIn(leak, out)
        self.assertEqual(bugs.template_message(msg, deny=deny), "")


class SecurityRoutingTests(unittest.TestCase):
    def test_security_routing(self):
        plain = {"kind": "crash", "frames": ["work.actor_str"], "event": ""}
        self.assertFalse(bugs.is_security(plain))
        self.assertFalse(bugs.is_security(plain, "the step failed twice"))
        self.assertTrue(bugs.is_security({**plain, "frames": ["work.x", "guard._check_sed"]}))
        self.assertTrue(bugs.is_security({**plain, "frames": ["scope.check"]}))
        self.assertTrue(bugs.is_security({"kind": "hook", "frames": ["hooks._guard"]}))
        self.assertTrue(bugs.is_security({"kind": "hook", "event": "guard", "frames": []}))
        self.assertTrue(bugs.is_security({"kind": "guard", "outcome": "allow"}))
        self.assertFalse(bugs.is_security({"kind": "guard", "outcome": "deny", "refusal_id": "_check_sed"}))
        self.assertTrue(bugs.is_security({**plain, "failopen": True}))
        self.assertTrue(bugs.is_security({**plain, "security": True}))
        for text in ("Bypass the check", "an allowlist entry", "INJECTED", "the tokens", "secrets", "exploitable",
                     "a credential", "the Password", "it leaks", "exfiltration", "path traversal", "escalated",
                     "privilege", "the sandbox", "a grant line"):
            with self.subTest(text=text):
                self.assertTrue(bugs.is_security(plain, text))
        # code whose failure can mean a bypass or a leak: lesson rules the guard runs, the grant code, the prompt
        # handler that reads grant lines, and the report builder, scrubber and recheck themselves
        for frame in ("lessonrules.check", "progress.use_grant", "progress._grant_lookup", "hooks.prompt",
                      "bugs.build_report", "bugs.scrub_text", "bugs.template_message", "bugs._recheck"):
            with self.subTest(frame=frame):
                self.assertTrue(bugs.is_security({**plain, "frames": ["work.x", frame]}))
        self.assertFalse(bugs.is_security({**plain, "frames": ["progress.step", "bugs.match"]}))

    def test_a_crash_whose_raw_message_is_about_secrets_goes_private(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(bugs, "BUGS", Path(d) / "bugs"), mock.patch.object(bugs, "CRASHES", Path(d) / "c"), \
                mock.patch.object(bugs, "install_channel", lambda: "release"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            bugs.handle_crash(["version"], KeyError("'api_password' is not in the map"))
            self.assertIn("private vulnerability reporting", err.getvalue())
            occ = bugs.occurrences()[-1]
        self.assertTrue(occ["security"])  # the template drops the quoted name: the raw message decides
        self.assertNotIn("password", json.dumps(occ))  # and the raw message is never kept in the occurrence


if __name__ == "__main__":
    unittest.main()
