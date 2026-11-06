# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The audit records spec (P-fxs8za703yx): schema v2, the hash chain and `dev audit verify`, retention, level,
# exports, queries and the redactor. One test per acceptance criterion; every test runs in a temporary folder.
import contextlib
import csv
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, progress  # noqa: E402

LIB = Path(__file__).resolve().parents[1] / "lib"
NOV6 = time.mktime((2026, 11, 6, 12, 0, 0, 0, 0, -1))

# The compliance review's redactor probe (Persephone, 32 cases). Every value is made up, and each is assembled at
# runtime so that the secret scan of this file never sees a whole credential. (name, text, part that must not survive)
A36 = "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8"
FAKE40 = "Zx9Wv8Ut7Sr6Qp5On4Ml3" + "Kj2Ih1Gf0EdCbA9876"
PEM_BODY = "MIIEowIBAAKCAQEAfake" + "fakefakefakefakefakefake0123456789"
VALUE = "S3cretFake" + "Value99"
PASS = "S3cret" + "FakePass"
PK = "PRIVATE" + " KEY"


def _pem(kind: str, end: bool = True) -> str:
    head = f"-----BEGIN {kind}{PK}"
    return f"{head}-----\n{PEM_BODY}\n-----END {kind}{PK}-----" if end else f"{head}-----\n{PEM_BODY}"


PROBE = [
    ("github ghp_", "token ghp_" + A36, A36[4:]),
    ("github_pat_", "github_pat_11ABCDEFG0" + A36, A36[8:]),
    ("slack xoxb", "xoxb-" + "1234567890-0987654321-FakeFakeFake", "0987654321-FakeFakeFake"),
    ("openai sk-proj", "sk-proj-" + A36, A36[4:]),
    ("anthropic sk-ant", "sk-ant-api03-" + A36, A36[4:]),
    ("aws AKIA", "AKIA" + "ABCDEFGHIJKLMNOP", "EFGHIJKLMNOP"),
    ("aws secret env", "AWS_SECRET_ACCESS_KEY=" + FAKE40, FAKE40[4:]),
    ("django SECRET_KEY", "SECRET_KEY=" + FAKE40, FAKE40[4:]),
    ("npm_", "npm_" + A36, A36[4:]),
    ("pypi-", "pypi-AgEIcHlwaS5vcmc" + A36, A36[4:]),
    ("google AIza", "AIza" + "SyFakeFakeFakeFakeFakeFakeFakeFake1", "FakeFakeFakeFakeFake"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "FakeSignatureFakeSig", "FakeSignatureFakeSig"),
    ("bearer header", "Authorization: Bearer " + "abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ("basic header", "Authorization: Basic " + "dXNlcjpwYXNzd29yZGZha2U=", "dXNlcjpwYXNzd29yZGZha2U="),
    ("pem rsa whole", _pem("RSA "), PEM_BODY),
    ("pem openssh", _pem("OPENSSH "), PEM_BODY),
    ("pem pkcs8 cut", _pem("", end=False), PEM_BODY),
    ("pem pgp block", _pem("PGP ").replace(PK + "-----", PK + " BLOCK-----"), PEM_BODY),
    ("url https user:pass", f"https://deploy:{PASS}@git.example.com/repo.git", PASS),
    ("url postgres", f"postgres://app:{PASS}@db.example.com:5432/app", PASS),
    ("url empty user", f"redis://:{PASS}@cache.example.com:6379", PASS),
    ("url pass with slash", "https://deploy:S3cret/FakePass@git.example.com/x", "S3cret/FakePass"),
    ("json client_secret", '{"client_secret": "' + VALUE + '"}', VALUE),
    ("json password", '{"password":"' + VALUE + '"}', VALUE),
    ("cli --password", f"mysql --password {VALUE} -h db", VALUE),
    ("env TOKEN=", "MY_API_TOKEN=" + VALUE, VALUE),
    ("stripe sk_live_", "sk_" + "live_" + A36, A36[4:]),
    ("gitlab glpat-", "glpat-" + "FakeFakeFake12345678", "FakeFake12345678"),
    ("huggingface hf_", "hf_" + A36, A36[4:]),
    ("google oauth ya29", "ya29." + "a0Af" + A36, A36[4:]),
    ("slack webhook", "https://hooks.slack.com/services/" + "T00000000/B00000000/FakeFakeFakeFakeFake1234",
     "FakeFakeFakeFakeFake1234"),
    ("azure AccountKey", "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + FAKE40 + "==", FAKE40[4:]),
]

# each rule the review added: one more credential of its shape, and text that only looks like one and must not change
LOOKALIKES = [
    ("key with a prefix or suffix", "export DB_PASSWORD_PROD=" + VALUE, VALUE,
     ["secrets: 0 found", "passwordless: true", "max_tokens: 4096", "the secretary: Ana", "TOKENS: 12"]),
    ("quoted key and value", "{'apiKey': '" + VALUE + " and more'}", VALUE,
     ['{"user": "ana", "tokens": 12}', "password reset: done"]),
    ("command-line flag", "gh auth login --with-token --api-key " + VALUE, VALUE,
     ["docker login --password-stdin ghcr.io", "claude --max-tokens 4096", "--token-file ~/.config/x"]),
    ("PGP private key block", _pem("PGP ").replace(PK + "-----", PK + " BLOCK-----"), PEM_BODY,
     ["-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQINBFakePublicKey"]),
    ("URL password with an @", "https://ci:Fake@Pass99" + "@git.example.com/r", "Fake@Pass99",
     ["http://localhost:4873/@scope/pkg", "https://registry.npmjs.org/@types/node", "https://ana@example.com/x",
      "git@github.com:acme/app.git", "http://example.com:8080?to=a@b.example"]),
    ("vendor tokens", "rk_" + "live_" + A36 + " dckr_" + "pat_" + A36 + " sk_" + "test_" + A36, A36[4:],
     ["hf_hub_download(repo)", "see sk_live in the Stripe docs", "ya29 is Google's prefix", "glpat- tokens expire"]),
    ("Slack webhook path", "https://hooks.slack.com/services/" + "T1/B2/FakeFakeFakeFakeFake9876", "FakeFake9876",
     ["https://hooks.slack.com/ is Slack's webhook host"]),
    ("Azure account key", "SharedAccessKey=" + FAKE40, FAKE40[4:], ["AccountName=devstore;EndpointSuffix=core.windows.net"]),
]


class AuditCase(unittest.TestCase):
    # every test gets its own audit folder, handoff repository and audit settings; the real trail is never touched
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = (audit.AUDIT, core.HANDOFF, core.CFG.get("audit"), progress.RUNS, progress.SESSIONS,
                      list(getattr(audit, "CHECKS", [])))
        self.env = {k: os.environ.get(k) for k in ("DEVKIT_FROM_AGENT", "DEVKIT_SESSION", "DEVKIT_AGENT")}
        audit.AUDIT = self.root / "state" / "audit"
        progress.RUNS, progress.SESSIONS = self.root / "state" / "runs", self.root / "state" / "sessions"
        core.HANDOFF = self.root / "handoff"
        core.CFG["audit"] = json.loads(json.dumps(core.DEFAULTS.get("audit") or {}))
        for k in self.env:
            os.environ.pop(k, None)

    def tearDown(self):
        audit.AUDIT, core.HANDOFF, cfg, progress.RUNS, progress.SESSIONS, checks = self.saved
        if hasattr(audit, "CHECKS"):
            audit.CHECKS[:] = checks
        if cfg is None:
            core.CFG.pop("audit", None)
        else:
            core.CFG["audit"] = cfg
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def policy(self, data: dict):
        f = core.HANDOFF / "policy" / "audit.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))

    def lines(self) -> list:
        return [json.loads(ln) for f in sorted(audit.AUDIT.glob("*.jsonl")) for ln in f.read_text().splitlines()]


class RedactionTests(AuditCase):
    # R8: the one redactor knows the K7 patterns; each value below is a made-up credential of that shape
    def test_k7_credentials_are_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        cases = {
            "Authorization: Bearer abcDEF123456ghiJKL789": "abcDEF123456ghiJKL789",
            f"token in a header {jwt}": jwt[20:],
            "npm_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8": "A1b2C3d4E5f6G7h8I9j0",
            "pypi-AgEIcHlwaS5vcmcCJDk5ZmQ1ZTVkLTM2YjUtNDc0Mi1hMDE0": "AgEIcHlwaS5vcmcCJDk5",
            "key AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY": "SyD-9tSrke72PouQMnMX",
            "git clone https://deploy:s3cr3tPassw0rd@github.com/acme/app.git": "s3cr3tPassw0rd",
        }
        for text, secret in cases.items():
            with self.subTest(text=text[:30]):
                out = audit.redact(text)
                self.assertNotIn(secret, out)
                self.assertIn("[redacted]", out)

    def test_bearer_keeps_its_scheme_and_a_url_keeps_its_host(self):
        self.assertEqual(audit.redact("Authorization: Bearer abcDEF123456ghiJKL789"),
                         "Authorization: Bearer …[redacted]")
        self.assertEqual(audit.redact("https://deploy:s3cr3tPassw0rd@github.com/acme/app.git"),
                         "https://deploy:…[redacted]@github.com/acme/app.git")

    def test_a_private_key_loses_its_whole_body(self):
        # a made-up key, split so that secret scanners do not flag this file
        key = ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
               "QyNTUxOQAAACBzZWNyZXRib2R5c2VjcmV0Ym9keXNlY3JldGJvZHk=\n-----END OPENSSH " + "PRIVATE KEY-----")
        out = audit.redact(f"found {key} in the diff")
        self.assertNotIn("b3BlbnNzaC1rZXkt", out)
        self.assertNotIn("QyNTUxOQAAACBz", out)
        self.assertTrue(out.startswith("found -----BEGIN OPENSSH PRIVATE KEY-----…[redacted]"))
        self.assertTrue(out.endswith(" in the diff"))
        # a key cut before its END line loses everything after BEGIN
        self.assertNotIn("b3BlbnNzaC1rZXkt", audit.redact(key[:90]))

    def test_secret_knows_every_k7_shape_but_not_its_own_marker(self):
        # the fixture tests assert SECRET finds nothing in redacted text, so it must find every raw shape
        for text in ("Bearer abcDEF123456ghiJKL789", "https://u:pw123456@h.example/x", "npm_" + "a1" * 18,
                     "AIza" + "b" * 35, "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N"):
            with self.subTest(text=text[:12]):
                self.assertIsNotNone(audit.SECRET.search(text))
                once = audit.redact(text)
                self.assertIsNone(audit.SECRET.search(once))
                self.assertEqual(audit.redact(once), once)

    def test_extra_patterns_from_the_team_policy_and_the_config(self):
        self.policy({"redact": {"extra": [r"ACME-[0-9]{6}"]}})
        core.CFG["audit"]["redact"] = {"extra": [r"client-[a-z]{5}", "("]}  # "(" is not a pattern: skipped
        out = audit.redact("ticket ACME-123456 for client-zorro")
        self.assertNotIn("ACME-123456", out)
        self.assertNotIn("client-zorro", out)
        self.assertEqual(audit.redact("plain words stay"), "plain words stay")

    def test_the_compliance_probe_secrets_are_all_redacted(self):
        # the review's 32 probe cases: none survives, the output is stable, and SECRET knows the raw shape but finds
        # nothing once it is redacted (the K7 fixture contract every other store relies on)
        for name, text, body in PROBE:
            with self.subTest(case=name):
                self.assertIsNotNone(audit.SECRET.search(text))
                out = audit.redact(text, extra=[])
                self.assertNotIn(body, out)
                self.assertIn("[redacted]", out)
                self.assertEqual(audit.redact(out, extra=[]), out)
                self.assertIsNone(audit.SECRET.search(out))

    def test_each_new_rule_catches_its_shape_and_spares_its_look_alikes(self):
        for name, text, body, alikes in LOOKALIKES:
            with self.subTest(rule=name):
                out = audit.redact(text, extra=[])
                self.assertNotIn(body, out)
                self.assertIsNone(audit.SECRET.search(out))
                for plain in alikes:
                    self.assertEqual(audit.redact(plain, extra=[]), plain)

    def test_no_prompt_text_reaches_a_record(self):
        audit.record("canary.miss", "devkit", "final message without the status block", "recorded",
                     prompt="please ship the secret roadmap", text="the secret roadmap")
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        self.assertNotIn("secret roadmap", raw)


class SchemaTests(AuditCase):
    # R1: one versioned schema, one registry of actions, extras only through declared fields, v1 still readable
    def crew_run(self):
        os.environ["DEVKIT_SESSION"] = "claude:sess-1"
        progress.SESSIONS.mkdir(parents=True)
        (progress.SESSIONS / "claude-sess-1.json").write_text(json.dumps(
            {"agent": "claude", "session": "sess-1", "project": "acme/app", "branch": "feat/x"}))
        progress.RUNS.mkdir(parents=True)
        (progress.RUNS / "claude-sess-1.json").write_text(json.dumps({
            "v": 2, "agent": "claude", "session": "sess-1", "project": "acme/app", "goal": "Invoice export",
            "agents": {"a1b2c3d4e5f6a7b8c9": {"persona": "Tank", "label": "Tank: the API", "parent": "main",
                                              "launch": "toolu_01ABC", "state": "running"}},
            "plan": [{"name": "design", "state": "done", "owner": "main"},
                     {"name": "api", "state": "running", "owner": "a1b2c3d4e5f6a7b8c9"}]}))

    def test_a_record_follows_schema_v2(self):
        audit.record("dev.task", "add x", "", "exit 2", duration=1.25, cwd="~/Development/acme/app", why="asked")
        audit.record("guard.deny", "rm -rf src", "steered to dev rm", "deny", tool="Bash", rule="rm")
        a, b = [r for r in self.lines() if r["action"] != "audit.config"]
        self.assertEqual((a["v"], b["v"]), (2, 2))
        self.assertEqual(audit.check_record(a), [])
        self.assertEqual(audit.check_record(b), [])
        self.assertLess(a["id"], b["id"])  # sortable by time, unique
        self.assertEqual(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(a["ts"])), a["time"][:19])
        self.assertTrue(a["time"].endswith("Z"))
        self.assertEqual(set(a["actor"]), {"user", "host", "agent", "session"})
        self.assertEqual((a["category"], a["severity"], a["outcome"]), ("change", "warning", "failed"))
        self.assertEqual(a["evidence"], {"duration": 1.25, "cwd": "~/Development/acme/app", "exit": 2})
        self.assertEqual(a["why"], "asked")
        self.assertEqual((b["category"], b["severity"], b["outcome"]), ("refusal", "warning", "blocked"))
        self.assertEqual(b["evidence"], {"tool": "Bash", "rule": "rm"})

    def test_a_crew_members_record_carries_its_context_and_member_finds_it(self):
        self.crew_run()
        os.environ["DEVKIT_FROM_AGENT"] = "a1b2c3d4e5f6a7b8c9"
        audit.record("dev.progress", 'step "api done"', "", "ok")
        os.environ.pop("DEVKIT_FROM_AGENT")
        audit.record("dev.task", "add y", "", "ok")  # Morpheus, in the same session
        tank = audit.query(member="Tank")
        self.assertEqual(len(tank), 1)
        r = tank[0]
        self.assertEqual(r["crew"], {"member": "tank", "role": "Backend engineer", "lane": "a1b2c3d4e5f6a7b8c9",
                                     "parent": "main", "tool_use_id": "toolu_01ABC"})
        self.assertEqual((r["project"], r["branch"], r["run"], r["step"]), ("acme/app", "feat/x", "Invoice export", "api"))
        main = audit.query(action="dev.task")[0]
        self.assertNotIn("crew", main)
        self.assertEqual((main["project"], main["run"]), ("acme/app", "Invoice export"))

    def test_a_hooks_agent_id_prefix_finds_the_lane(self):
        self.crew_run()
        audit.record("crew.bind", "Tank", "Tank: the API", agent_id="a1b2c3d4e5f6a7b8", launch="toolu_01ABC")
        crew = audit.query(action="crew.bind")[-1]["crew"]
        self.assertEqual((crew["member"], crew["lane"]), ("tank", "a1b2c3d4e5f6a7b8c9"))

    def test_extras_go_only_into_declared_fields(self):
        audit.record("plan.done", "api", "", "ok", project="acme/app", run="Invoice export", by="Morpheus",
                     was="running", id="M3", sha="abc1234", member="Neo", rules=["scope", "lane"], bogus="x" * 9)
        r = audit.query(action="plan.done")[-1]
        self.assertTrue(set(r) <= set(audit.SCHEMA), set(r) - set(audit.SCHEMA))
        self.assertEqual(r["ref"], {"by": "Morpheus", "was": "running", "id": "M3"})
        self.assertEqual(r["evidence"], {"commit": "abc1234", "rule": "scope, lane"})
        self.assertEqual(r["crew"]["member"], "neo")
        self.assertNotIn("xxxxxxxxx", json.dumps(r))

    def test_every_action_the_code_records_is_registered(self):
        found = set()
        call = re.compile(r"""(?:audit\.record|_audit)\(\s*f?["']([a-z][a-z0-9_.-]*)""")
        for f in sorted((LIB / "devkit").glob("*.py")):
            found |= {m.group(1).rstrip(".") for m in call.finditer(f.read_text())}
        found.discard("action")
        self.assertGreater(len(found), 25)
        for action in sorted(found):
            with self.subTest(action=action):
                self.assertTrue(audit.registered(action), f"{action} is not in audit.ACTIONS")

    def test_every_keyword_the_code_passes_is_declared(self):
        found = {}
        for f in sorted((LIB / "devkit").glob("*.py")):
            s = f.read_text()
            for m in re.finditer(r"(?:audit\.record|\b_audit)\(", s):
                i, depth = m.end(), 1
                while depth and i < len(s):
                    depth += {"(": 1, ")": -1}.get(s[i], 0)
                    i += 1
                call = re.sub(r"\([^()]*\)", "()", re.sub(r"\([^()]*\)", "()", s[m.end():i - 1]))
                for k in re.findall(r"(?:^|,)\s*([a-z_]+)=", call):
                    found.setdefault(k, f.name)
        found.pop("outcome", None)
        self.assertIn("verified", found)
        for k, where in sorted(found.items()):
            with self.subTest(keyword=k, file=where):
                self.assertIn(k, audit.EXTRAS, f"{where} passes {k}=, which audit.EXTRAS does not declare")

    def test_readers_accept_v1_records(self):
        audit.AUDIT.mkdir(parents=True)
        v1 = {"ts": time.time() - 60, "user": "ana", "host": "mac", "agent": "claude", "session": "962d6aba-c6a",
              "action": "crew.gate", "target": "acme/app", "detail": "Tank: scope", "outcome": "deny",
              "member": "Tank", "rules": ["scope"]}
        (audit.AUDIT / time.strftime("%Y-%m.jsonl")).write_text(json.dumps(v1) + "\n")
        r = audit.query(member="tank")[0]
        self.assertEqual((r["v"], r["actor"]["user"], r["outcome"], r["category"]), (1, "ana", "blocked", "refusal"))
        self.assertEqual(r["crew"]["member"], "tank")
        self.assertEqual(r["evidence"]["rule"], "scope")
        self.assertEqual(audit.records(1, action="crew")[0]["target"], "acme/app")  # the old reader's API


class PersonalDataTests(AuditCase):
    # the compliance review, section 2: a record keeps who and what, never the text people wrote to each other, and
    # never the OS user name inside a home path; the same holds for v1 records when they are read or exported
    def test_a_crew_message_keeps_its_id_and_length_but_not_its_text(self):
        # progress.crew_ask records each message this way; crew messages quote the developer's requests
        text = "Ana asked for the Acme roadmap by Friday, please"
        audit.record("crew.message", "Neo → Persephone", text[:300], project="acme/app", id="M12")
        r = audit.query(action="crew.message")[-1]
        self.assertNotIn("roadmap", json.dumps(r))
        self.assertEqual((r["target"], r["ref"]["id"]), ("Neo → Persephone", "M12"))
        self.assertIn(f"{len(text)} characters", r["detail"])
        audit.record("crew.message", "Neo → Morpheus", "x" * 300, id="M13")  # progress cuts long text at 300
        self.assertIn("300+ characters", audit.query(action="crew.message")[-1]["detail"])

    def test_dev_crew_ask_and_answer_keep_their_arguments_but_not_the_message(self):
        # cli.py records `dev crew …` with its arguments joined by spaces
        cases = {
            "ask --to Persephone please confirm the Acme roadmap": "ask --to Persephone",
            "answer M12 yes, the roadmap for Acme is fine": "answer M12",
            "ask --decision which roadmap for Acme? --options A: ship | B: wait --recommend A --irreversible push":
                "ask --decision --recommend A --irreversible push",
            "--json ask --to=neo the roadmap": "--json ask --to=neo",
        }
        for target, kept in cases.items():
            with self.subTest(target=target[:20]):
                audit.record("dev.crew", target, "", "ok")
                got = audit.query(action="dev.crew")[-1]["target"]
                self.assertTrue(got.startswith(kept + " "), got)
                self.assertNotIn("roadmap", got)
                self.assertIn("characters", got)
        for target in ("decide D3 B", "mode auto --approve push", "delegate api --to Tank"):
            with self.subTest(target=target):
                audit.record("dev.crew", target, "", "ok")
                self.assertEqual(audit.query(action="dev.crew")[-1]["target"], target)

    def test_run_step_and_branch_from_the_session_are_redacted(self):
        os.environ["DEVKIT_SESSION"] = "claude:sess-2"
        progress.SESSIONS.mkdir(parents=True)
        (progress.SESSIONS / "claude-sess-2.json").write_text(json.dumps(
            {"agent": "claude", "session": "sess-2", "project": "acme/app", "branch": "fix/password=" + VALUE}))
        progress.RUNS.mkdir(parents=True)
        (progress.RUNS / "claude-sess-2.json").write_text(json.dumps({
            "v": 2, "agent": "claude", "session": "sess-2", "project": "acme/app",
            "goal": "deploy with ghp_" + A36, "agents": {},
            "plan": [{"name": "rotate token=" + VALUE, "state": "running", "owner": "main"}]}))
        audit.record("dev.task", "add x", "", "ok")
        r = audit.query(action="dev.task")[-1]
        for k in ("run", "step", "branch"):
            with self.subTest(field=k):
                self.assertIn("[redacted]", r[k])
        self.assertNotIn(A36[4:], json.dumps(r))
        self.assertNotIn(VALUE, json.dumps(r))

    def test_the_home_folder_becomes_tilde_in_text_fields(self):
        home = str(core.HOME)
        audit.record("dev.rm", f"{home}/Development/acme/app/build", f"moved to {home}/.Trash", "ok",
                     why=f"cleanup of {home}/tmp", cwd=f"{home}/Development")
        audit.record("dev.mv", "/Users/jane.doe/Development/x /Users/Shared/x", "", "ok")
        rm, mv = audit.query(action="dev.rm")[-1], audit.query(action="dev.mv")[-1]
        self.assertEqual((rm["target"], rm["detail"], rm["why"], rm["evidence"]["cwd"]),
                         ("~/Development/acme/app/build", "moved to ~/.Trash", "cleanup of ~/tmp", "~/Development"))
        self.assertEqual(mv["target"], "~/Development/x /Users/Shared/x")  # a shared folder names nobody

    def test_v1_records_are_redacted_when_read_and_in_every_export(self):
        audit.AUDIT.mkdir(parents=True)
        v1 = {"ts": time.time() - 60, "user": "fixture-user", "host": "fixture-host", "agent": "claude",
              "session": "abc123", "action": "dev.crew",
              "target": "ask --to neo Authorization: Bearer " + "abcdefghijklmnop1234",
              "detail": "run on /Users/fixture-user/Development/x", "outcome": "ok", "why": "token=" + VALUE}
        (audit.AUDIT / time.strftime("%Y-%m.jsonl")).write_text(json.dumps(v1) + "\n")
        recs = audit.query(action="dev.crew")
        self.assertEqual((len(recs), recs[0]["v"]), (1, 1))
        self.assertEqual(recs[0]["detail"], "run on ~/Development/x")
        for fmt in audit.FORMATS:
            with self.subTest(format=fmt):
                out = audit.render(fmt, recs)
                self.assertNotIn("abcdefghijklmnop1234", out)
                self.assertNotIn(VALUE, out)
                self.assertNotIn("/Users/fixture-user", out)
                self.assertIn("fixture-host", out)  # who acted stays: the actor is not text people wrote


class IntegrityTests(AuditCase):
    # R2: the hash chain, file modes, `dev audit verify`, and the published copy as the anchor
    def month(self) -> Path:
        return audit.AUDIT / time.strftime("%Y-%m.jsonl")

    def write(self, n=3):
        for i in range(n):
            audit.record("dev.task", f"add {i}", f"detail {i}", "ok")

    def chain(self) -> list:
        return [r for r in self.lines() if r["action"] == "dev.task"]

    def test_files_are_0600_in_a_0700_folder(self):
        self.write(1)
        self.assertEqual(stat.S_IMODE(audit.AUDIT.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.month().stat().st_mode), 0o600)

    def test_an_existing_0644_file_becomes_0600(self):
        audit.AUDIT.mkdir(parents=True, mode=0o755)
        os.chmod(audit.AUDIT, 0o755)
        self.month().write_text("")
        os.chmod(self.month(), 0o644)
        self.write(1)
        self.assertEqual(stat.S_IMODE(self.month().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(audit.AUDIT.stat().st_mode), 0o700)

    def test_each_record_links_to_the_one_before(self):
        self.write(3)
        recs = self.lines()
        self.assertEqual(recs[0]["prev"], "")
        for before, after in zip(recs, recs[1:]):
            self.assertEqual(after["prev"], before["hash"])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_editing_one_character_fails_verify_at_that_record(self):
        self.write(3)
        lines = self.month().read_text().splitlines()
        n = next(i for i, ln in enumerate(lines) if "detail 1" in ln)
        lines[n] = lines[n].replace("detail 1", "detail 7")
        self.month().write_text("\n".join(lines) + "\n")
        res = audit.verify(team=False)
        self.assertFalse(res["ok"])
        self.assertEqual((res["first"]["line"], res["first"]["kind"]), (n + 1, "edited"))
        self.assertEqual(res["first"]["id"], json.loads(lines[n])["id"])

    def test_a_removed_record_breaks_the_next_link(self):
        self.write(3)
        lines = self.month().read_text().splitlines()
        n = next(i for i, ln in enumerate(lines) if "detail 1" in ln)
        self.month().write_text("\n".join(lines[:n] + lines[n + 1:]) + "\n")
        res = audit.verify(team=False)
        self.assertEqual((res["first"]["line"], res["first"]["kind"]), (n + 1, "unlinked"))

    def test_the_first_v2_record_seals_the_v1_lines_before_it(self):
        audit.AUDIT.mkdir(parents=True)
        old = [json.dumps({"ts": time.time() - i, "user": "ana", "host": "mac", "action": "dev.task",
                           "target": f"t{i}", "outcome": "ok"}) for i in range(3)]
        self.month().write_text("\n".join(old) + "\n")
        self.write(1)
        self.assertTrue(audit.verify(team=False)["ok"])
        self.month().write_text(self.month().read_text().replace('"t1"', '"t9"'))
        res = audit.verify(team=False)
        self.assertEqual((res["first"]["line"], res["first"]["kind"]), (4, "unlinked"))

    def test_concurrent_writers_keep_one_chain(self):
        audit.record("dev.task", "first")  # the settings record is written once, before the writers start
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from devkit import audit; "
                "audit.AUDIT = Path(sys.argv[2]); [audit.record('dev.task', f'w{i}') for i in range(40)]")
        env = {k: v for k, v in os.environ.items() if k not in core.AGENT_MARKERS}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(LIB), str(audit.AUDIT)], env=env)
                 for _ in range(4)]
        self.assertEqual([p.wait(60) for p in procs], [0] * 4)
        self.assertEqual(len(self.chain()), 161)
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res.get("first"))

    def test_the_published_copy_anchors_the_tail(self):
        self.write(3)
        self.assertEqual(len(audit.publish()), 1)
        self.write(2)
        res = audit.verify()
        self.assertTrue(res["ok"])
        self.assertEqual(res["unpublished"], 2)  # chained, but not yet anchored: reported, never called verified
        lines = self.month().read_text().splitlines()
        self.month().write_text("\n".join(lines[:2]) + "\n")  # the local file cut below the published head
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual(res["first"]["kind"], "diverged")
        published = audit.team_dir() / self.month().name
        before = published.read_text()
        self.assertEqual(audit.publish(), [])  # a diverging file never overwrites the anchor
        self.assertEqual(published.read_text(), before)

    def test_plug_in_checks_run_on_each_intact_record(self):
        audit.record("crew.decided", "D1", "push", evidence={"channel": "E2", "transcript_uuid": "u-missing"})
        audit.record("crew.decided", "D2", "tag", evidence={"channel": "E2", "transcript_uuid": "u-1"})
        audit.CHECKS.append(lambda rec, path, line: "no such user message"
                            if (rec.get("evidence") or {}).get("transcript_uuid") == "u-missing" else None)
        res = audit.verify(team=False)
        self.assertFalse(res["ok"])
        first = res["first"]
        self.assertEqual((first["kind"], first["problem"]), ("check", "no such user message"))
        self.assertEqual(json.loads(self.month().read_text().splitlines()[first["line"] - 1])["target"], "D1")


class RetentionTests(AuditCase):
    # R3: configurable retention with a floor, a legal hold, and a record of every prune; R7's floor from the team
    def months(self, stems):
        audit.AUDIT.mkdir(parents=True, exist_ok=True)
        for st in stems:
            (audit.AUDIT / f"{st}.jsonl").write_text(json.dumps({"ts": 1, "action": "dev.task"}) + "\n")

    def test_the_defaults(self):
        # the team copy is the more widely read store, so it keeps no longer than this machine (compliance review)
        s = audit.settings()
        self.assertEqual((s["retention"]["local_months"], s["retention"]["team_months"],
                          s["retention"]["floor_months"], s["hold"], s["level"]), (13, 13, 3, False, "changes"))

    def test_a_floor_under_three_months_is_refused(self):
        # 3 months is a hard minimum: it covers noticing an incident and investigating it; less data comes from
        # audit.level security, not from a shorter floor. local and team months still take 1 (raised to the floor)
        for v in (1, 2):
            with self.subTest(floor=v):
                errors = core.config_errors({"audit": {"retention": {"floor_months": v}}})
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("audit.retention.floor_months", errors[0])
        self.assertEqual(core.config_errors({"audit": {"retention": {"floor_months": 3, "local_months": 1,
                                                                     "team_months": 1}}}), [])
        core.CFG["audit"]["retention"]["floor_months"] = 2
        self.assertEqual(audit.settings()["retention"]["floor_months"], 3)
        self.policy({"retention": {"floor_months": 1}})
        s = audit.settings()
        self.assertEqual(s["retention"]["floor_months"], 3)
        self.assertTrue(any("policy" in e and "floor_months" in e for e in s["errors"]), s["errors"])

    def test_one_month_keeps_the_floor_and_records_the_prune(self):
        core.CFG["audit"]["retention"]["local_months"] = 1
        self.months(["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"])
        out = audit.prune(now=NOV6)
        self.assertEqual(sorted(f.stem for f in audit.AUDIT.glob("*.jsonl")),
                         ["2026-08", "2026-09", "2026-10", "2026-11"])
        self.assertEqual(out["removed"], ["2026-05", "2026-06", "2026-07"])
        rec = audit.query(action="audit.prune")[-1]
        self.assertEqual(rec["category"], "lifecycle")
        self.assertIn("3 file(s)", rec["detail"])
        self.assertIn("kept 3 month(s)", rec["detail"])

    def test_a_hold_stops_all_pruning(self):
        core.CFG["audit"]["hold"] = True
        self.months(["2020-01", "2026-11"])
        self.assertEqual(audit.prune(now=NOV6)["removed"], [])
        self.assertTrue((audit.AUDIT / "2020-01.jsonl").exists())
        self.assertIn("hold", audit.query(action="audit.prune")[-1]["detail"])

    def test_team_months_prune_this_machines_handoff_copy(self):
        core.CFG["audit"]["retention"]["team_months"] = 6
        mine = audit.team_dir()
        mine.mkdir(parents=True)
        other = core.HANDOFF / "audit" / "bea@mini"
        other.mkdir(parents=True)
        for d in (mine, other):
            for st in ("2025-01", "2026-10"):
                (d / f"{st}.jsonl").write_text("{}\n")
        out = audit.prune(now=NOV6)
        self.assertEqual([p.name for p in out["team"]], ["2025-01.jsonl"])
        self.assertFalse((mine / "2025-01.jsonl").exists())
        self.assertTrue((other / "2025-01.jsonl").exists())  # another machine's folder is its own

    def test_a_policy_asking_for_less_changes_nothing(self):
        core.CFG["audit"]["hold"] = True
        self.policy({"level": "security", "hold": False, "publish": False,
                     "retention": {"local_months": 1, "team_months": 1, "floor_months": 1}})
        s = audit.settings()
        self.assertEqual((s["retention"]["local_months"], s["retention"]["team_months"],
                          s["retention"]["floor_months"], s["hold"], s["level"], s["publish"]),
                         (13, 13, 3, True, "changes", True))

    def test_the_team_policy_raises_the_machine(self):
        self.policy({"level": "all", "hold": True, "retention": {"floor_months": 18}})
        s = audit.settings()
        self.assertEqual((s["retention"]["local_months"], s["retention"]["floor_months"], s["hold"], s["level"]),
                         (18, 18, True, "all"))

    def test_invalid_settings_are_reported_and_fall_back(self):
        core.CFG["audit"].update(level="loud", hold="yes", retention={"local_months": 0, "team_months": "x"})
        errors = core.config_errors({"audit": core.CFG["audit"]})
        self.assertEqual(len(errors), 4, errors)
        s = audit.settings()
        self.assertEqual((s["level"], s["hold"], s["retention"]["local_months"], s["retention"]["team_months"]),
                         ("changes", False, 13, 13))
        self.assertEqual(len(s["errors"]), 4)


class LevelTests(AuditCase):
    # R4: changes (default), security, all; refusals, overrides and safeguard changes whatever the level
    def test_security_keeps_refusals_overrides_and_safeguard_changes_only(self):
        core.CFG["audit"]["level"] = "security"
        audit.record("dev.task", "add x")
        audit.record("handoff.tasks", "tasks.md")
        audit.record("guard.deny", "rm -rf src", "", "deny")
        audit.record("raci.override", "api", "Tank: urgent", "override", why="urgent")
        audit.record("config.blocked", "settings.json", "DEVKIT_GUARD", "blocked")
        audit.record("dev.jobs", "set gc", "", "ok", why="quieter hour")
        kept = [r["action"] for r in self.lines() if r["action"] != "audit.config"]
        self.assertEqual(kept, ["guard.deny", "raci.override", "config.blocked", "dev.jobs"])

    def test_all_records_read_only_commands(self):
        self.assertFalse(audit.audited(["status"]))
        core.CFG["audit"]["level"] = "all"
        self.assertTrue(audit.audited(["status"]))
        self.assertTrue(audit.audited(["audit", "stats"]))
        self.assertFalse(audit.audited(["__complete", "dev"]))
        self.assertFalse(audit.audited(["hook", "guard"]))

    def test_a_change_of_the_audit_settings_is_recorded(self):
        audit.record("dev.task", "add x")
        core.CFG["audit"]["level"] = "security"
        core.CFG["audit"]["retention"]["local_months"] = 24
        audit.record("guard.deny", "rm x", "", "deny")
        changes = audit.query(action="audit.config")
        self.assertEqual(len(changes), 1)  # default settings at the start are not a change
        self.assertIn("level: changes → security", changes[-1]["detail"])
        self.assertIn("retention.local_months: 13 → 24", changes[-1]["detail"])
        self.assertEqual(changes[-1]["category"], "config")


class ExportTests(AuditCase):
    # R5: JSONL (canonical), CSV for a spreadsheet, OCSF and CEF for a SIEM, and audit.forward
    def setUp(self):
        super().setUp()
        audit.record("dev.task", "=HYPERLINK(\"http://x\")", 'a "quoted", comma\nand a new line', "ok")
        audit.record("guard.deny", "rm -rf src | tee", "key=value \\ back", "deny", tool="Bash")

    def test_ocsf_events_are_valid(self):
        events = [json.loads(ln) for ln in audit.export("ocsf", action="dev.task").splitlines()]
        events += [json.loads(ln) for ln in audit.export("ocsf", action="guard").splitlines()]
        self.assertEqual(len(events), 2)
        for e in events:
            with self.subTest(event=e["message"][:20]):
                for k in ("activity_id", "category_uid", "class_uid", "metadata", "severity_id", "time", "type_uid"):
                    self.assertIn(k, e)  # the OCSF base event's required attributes
                self.assertEqual(e["type_uid"], e["class_uid"] * 100 + e["activity_id"])
                self.assertIn(e["severity_id"], range(0, 7))
                self.assertIn(e["status_id"], (0, 1, 2, 99))
                self.assertIsInstance(e["time"], int)
                self.assertEqual(e["metadata"]["product"]["name"], "devkit")
                self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", e["metadata"]["version"]))
                self.assertTrue(e["metadata"]["uid"])
        self.assertEqual((events[1]["status_id"], events[1]["severity_id"]), (2, 3))

    def test_csv_opens_in_a_spreadsheet(self):
        rows = list(csv.reader(io.StringIO(audit.export("csv", action="dev.task"))))
        self.assertEqual(rows[0][:4], ["id", "time", "user", "host"])
        self.assertEqual(len(rows), 2)
        self.assertEqual({len(r) for r in rows}, {len(rows[0])})
        col = rows[0].index
        self.assertEqual(rows[1][col("detail")], 'a "quoted", comma\nand a new line')
        self.assertEqual(rows[1][col("target")], "'=HYPERLINK(\"http://x\")")  # no formula runs in the sheet

    def test_cef_lines_escape_their_separators(self):
        lines = audit.export("cef", action="guard").splitlines()
        self.assertEqual(len(lines), 1)
        head = re.split(r"(?<!\\)\|", lines[0])
        self.assertEqual(head[:4], ["CEF:0", "Firefly Software Solutions", "devkit", core.VERSION])
        self.assertEqual((head[4], head[6]), ("guard.deny", "6"))
        self.assertIn(r"rm -rf src \| tee", lines[0])
        self.assertIn(r"key\=value \\ back", lines[0])

    def test_jsonl_is_the_stored_records_and_the_window_filters(self):
        out = [json.loads(ln) for ln in audit.export("jsonl").splitlines()]
        self.assertEqual(out, self.lines())
        self.assertEqual(audit.export("jsonl", until="2020-01-01"), "")
        self.assertEqual(len(audit.export("jsonl", since=time.strftime("%Y-%m-%d")).splitlines()), len(out))

    def test_an_export_file_is_private_and_recorded(self):
        out = self.root / "out" / "audit.csv"
        n = audit.export_to("csv", out, action="dev.task")
        self.assertEqual(n, 1)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(audit.query(action="audit.export")[-1]["evidence"]["count"], 1)

    def test_forward_to_a_file_and_to_syslog(self):
        sink = self.root / "siem" / "devkit.jsonl"
        core.CFG["audit"]["forward"] = str(sink)
        audit.record("dev.task", "add z")
        self.assertEqual(json.loads(sink.read_text().splitlines()[-1])["target"], "add z")
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv.settimeout(5)
        core.CFG["audit"]["forward"] = f"syslog://127.0.0.1:{srv.getsockname()[1]}"
        audit.record("dev.task", "add w")
        data = ""
        while "add w" not in data:  # the settings change is forwarded first
            data = srv.recv(65536).decode()
        srv.close()
        self.assertIn("CEF:0|Firefly Software Solutions|devkit|", data)


class QueryTests(AuditCase):
    # R6: filters, show, stats and config
    def setUp(self):
        super().setUp()
        audit.record("dev.task", "add x", "", "ok", project="acme/app")
        audit.record("guard.deny", "rm -rf src", "", "deny", project="acme/web")
        audit.record("dev.push", "origin feat/x", "rejected", "exit 1", project="acme/app")

    def q(self, **kw) -> list:
        return [r["action"] for r in audit.query(**kw) if r["action"] != "audit.config"]

    def test_filters(self):
        self.assertEqual(self.q(project="acme/app"), ["dev.task", "dev.push"])
        self.assertEqual(self.q(category="refusal"), ["guard.deny"])
        self.assertEqual(self.q(severity="warning"), ["guard.deny", "dev.push"])  # warning and above
        self.assertEqual(self.q(outcome="failed"), ["dev.push"])
        self.assertEqual(self.q(grep="REJECT"), ["dev.push"])
        self.assertEqual(self.q(since=time.strftime("%Y-%m-%d"))[:1], ["dev.task"])
        self.assertEqual(self.q(until="2020-01-01"), [])

    def test_show_finds_a_record_by_id_or_unique_prefix(self):
        r = audit.query(action="guard")[0]
        self.assertEqual(audit.show(r["id"])["target"], "rm -rf src")
        ids = [x["id"] for x in audit.query()]
        n = next(n for n in range(1, 27) if sum(i.startswith(r["id"][:n]) for i in ids) == 1)
        self.assertEqual(audit.show(r["id"][:n].lower())["id"], r["id"])
        self.assertIsNone(audit.show(r["id"][:1]))  # every id here starts with it: ambiguous
        self.assertIsNone(audit.show("nope"))

    def test_stats_count_per_category_and_day(self):
        st = audit.stats(audit.query())
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(st["categories"]["change"], 2)
        self.assertEqual(st["categories"]["refusal"], 1)
        self.assertEqual(st["days"][day]["change"], 2)

    def test_config_says_where_records_live_their_size_and_the_next_prune(self):
        c = audit.config_report(now=NOV6)
        self.assertEqual(c["settings"]["level"], "changes")
        self.assertEqual(c["local"]["path"], str(audit.AUDIT))
        self.assertGreater(c["local"]["bytes"], 0)
        self.assertEqual(c["local"]["files"], 1)
        self.assertEqual(c["next_prune"]["keeps_from"], "2025-10")


class CommandTests(AuditCase):
    # the `dev audit` command, through audit.add_arguments and audit.cmd_audit (cli.py wires them)
    def run_cmd(self, *argv):
        import argparse
        import contextlib
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = audit.cmd_audit(a)
        return rc, out.getvalue()

    def test_verify_exits_1_at_a_broken_link_and_records_the_check(self):
        audit.record("dev.task", "add 1")
        audit.record("dev.task", "add 2")
        self.assertEqual(self.run_cmd("verify")[0], 0)
        f = audit.AUDIT / time.strftime("%Y-%m.jsonl")
        f.write_text(f.read_text().replace("add 1", "add 9"))
        rc, out = self.run_cmd("verify")
        self.assertEqual(rc, 1)
        self.assertIn("line 1", out)
        last = [json.loads(ln) for ln in f.read_text().splitlines()][-1]
        self.assertEqual((last["action"], last["outcome"], last["severity"]), ("audit.verify", "failed", "critical"))

    def test_export_writes_the_file_and_show_and_bad_input_exit_nonzero(self):
        audit.record("guard.deny", "rm -rf src", "", "deny")
        out = self.root / "x.ocsf"
        rc, text = self.run_cmd("export", "--format", "ocsf", "--action", "guard", "-o", str(out))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.read_text().splitlines()[0])["class_uid"], 0)
        rid = audit.query(action="guard")[0]["id"]
        rc, text = self.run_cmd("show", rid)
        self.assertEqual((rc, json.loads(text)["id"]), (0, rid))
        self.assertEqual(self.run_cmd("show", "ZZZZ")[0], 1)
        self.assertEqual(self.run_cmd("export", "--format", "xml")[0], 2)
        self.assertEqual(self.run_cmd("nope")[0], 2)
        self.assertEqual(self.run_cmd("--since", "yesterday")[0], 2)

    def test_queries_print_their_records(self):
        audit.record("dev.task", "add x", "", "ok", project="acme/app")
        rc, text = self.run_cmd("--project", "acme/app", "--format", "csv")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual((rc, len(rows), rows[1][rows[0].index("target")]), (0, 2, "add x"))
        rc, text = self.run_cmd("stats", "--json")
        self.assertEqual((rc, json.loads(text)["categories"]["change"]), (0, 1))
        rc, text = self.run_cmd("config")
        self.assertEqual(rc, 0)
        self.assertIn("13 months here", text)

    def test_config_says_git_history_keeps_pruned_months(self):
        # pruning removes files from the handoff working copy only, so team_months is not yet a real limit: the
        # settings must say so rather than promise a deletion that does not happen (compliance review, section 1)
        rc, text = self.run_cmd("config")
        self.assertEqual(rc, 0)
        self.assertIn("git history keeps every published month", text)
        rc, text = self.run_cmd("config", "--json")
        self.assertIn("git history keeps every published month", json.loads(text)["team"]["history"])


class CliWiringTests(unittest.TestCase):
    # `dev audit` must reach the new subcommands, and `dev config validate` must check the audit keys
    def test_dev_audit_parses_the_new_subcommands_and_filters(self):
        from devkit import cli
        a = cli.build_parser().parse_args(["audit", "export", "--format", "ocsf", "--member", "Tank", "-o", "x.json"])
        self.assertEqual((a.audit_cmd, a.format, a.member, a.out), ("export", "ocsf", "Tank", "x.json"))
        self.assertEqual(a.fn.__module__, "devkit.audit")

    def test_config_validate_reports_an_audit_setting_below_the_floor(self):
        from devkit import cli, core
        bad = {"audit": {"retention": {"local_months": 0}}}
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "config.json")
            with open(f, "w") as fh:
                json.dump(bad, fh)
            saved = core.CONFIG_FILE
            core.CONFIG_FILE = type(saved)(f)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    rc = cli._config_validate()
            finally:
                core.CONFIG_FILE = saved
        self.assertNotEqual(rc, 0)
        self.assertIn("audit", out.getvalue())


if __name__ == "__main__":
    unittest.main()
