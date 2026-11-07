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
import shlex
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

# Seraph's review batch 3 (M244): command and header forms whose value survived, each with the text that only looks
# like it and must not change. The member gate records the words of the command it refuses, so a command is text too.
CMD = "Fk9" + "Vq2Zt7Lm"
CURL = "cur" + "l"  # the secret scan of this file reads "curl -u user:value" as a credential, whatever the value
COMMANDS = [
    ("curl -u", f"{CURL} -sS -u admin:{CMD} https://api.example.com/x", CMD,
     ["curl -u admin https://api.example.com:8443/x", "docker run -u 1000:1000 alpine", "sort -u a.txt b:c.txt"]),
    ("sshpass -p", f"sshpass -P assword -p '{CMD}' ssh deploy@host", CMD,
     ["sshpass -e ssh -p 2222 deploy@host", "sshpass -f ~/.pw ssh host", "ssh -p 2222 deploy@host"]),
    ("mysql -p", f"mysql -u root -p{CMD} app", CMD,
     ["mysql -u root -p app", "mysql -h db -P 3306 -u root -p", "mysqldump --single-transaction app"]),
    ("docker login -p", f"docker login -u ana -p {CMD} ghcr.io", CMD,
     ["docker login -u ana --password-stdin ghcr.io", "docker run -p 8080:80 nginx", "docker login ghcr.io"]),
    ("Authorization: token", f"curl -H 'Authorization: token {CMD}' https://api.github.com/user", CMD,
     ["Authorization: token required", "the token authorization step"]),
    ("gh secret set --body", f'gh secret set NPM_TOKEN --repo acme/app --body "{CMD}"', CMD,
     ['gh pr create --title fix --body "Fixes the login bug"', "gh secret set NPM_TOKEN < token.txt",
      "gh secret list --repo acme/app"]),
    ("passphrase", f"ssh_passphrase={CMD}", CMD, ["a passphrase-protected key", "passphrases: 2"]),
    ("credentials", f"credentials: {CMD}", CMD,
     ["git config credential.helper osxkeychain", "credential.helper=store", "credentialed: yes"]),
    # Seraph's batch 4, follow-up 5: a value echoed into a login that reads its secret from standard input
    ("echo | --password-stdin", f"echo -n '{CMD}' | docker login -u ana --password-stdin ghcr.io", CMD,
     ["echo done | tee build.log", "cat ~/.pw | docker login --password-stdin ghcr.io",
      "aws ecr get-login-password | docker login --username AWS --password-stdin 1234.dkr.ecr.example.com",
      "echo ok; docker login --password-stdin ghcr.io", "echo ok || docker login --password-stdin ghcr.io"]),
    ("printf | --with-token", f"printf '%s' {CMD}|gh auth login --with-token", CMD,
     ["printf '%s\\n' done | tee build.log", "gh auth login --with-token < token.txt"]),
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

    def test_commands_and_headers_that_carry_a_secret_lose_it_and_their_look_alikes_stay(self):
        for name, text, body, alikes in COMMANDS:
            with self.subTest(rule=name):
                self.assertIsNotNone(audit.SECRET.search(text))
                out = audit.redact(text, extra=[])
                self.assertNotIn(body, out)
                self.assertIn("[redacted]", out)
                self.assertEqual(audit.redact(out, extra=[]), out)
                self.assertIsNone(audit.SECRET.search(out))
                for plain in alikes:
                    self.assertEqual(audit.redact(plain, extra=[]), plain)
        # what the gate refuses reaches the trail through the real record
        audit.record("crew.gate", f"gh secret set NPM_TOKEN --body {CMD}", f"refused: sshpass -p {CMD} ssh host",
                     "deny")
        self.assertNotIn(CMD, (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text())

    def test_the_redactor_runs_in_linear_time(self):
        # 64 KB of text that makes each command rule start a scan at every word; a rule that backtracks takes minutes
        n = 1 << 16
        texts = ["curl -u " * (n // 8), "sshpass -P x " * (n // 13), "mysql -u " * (n // 9),
                 "docker login -u " * (n // 16), "Authorization: token " + "a" * n, "gh secret set X " * (n // 16),
                 "passphrase credentials " * (n // 23), "https://u:" + "@" * n, "a" * n,
                 "echo x | sudo " * (n // 14)]
        start = time.perf_counter()
        for t in texts:
            audit.redact(t, extra=[])
            audit.SECRET.search(t)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, f"{elapsed:.2f}s for {len(texts)} texts of 64 KB")

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

    def test_the_gate_and_scope_records_keep_their_fields(self):
        # guard._gate_record, guard.bypass and Tank's scope.write detection pass these; EXTRAS dropped them
        audit.record("guard.irreversible", "git push origin main", "push", "deny", irreversible="push", tier=2,
                     repo="acme/app", member="tank", setting="enforce", grant="G3", source="E2", rule="tier-2")
        audit.record("guard.bypass", "git push", "the developer's hatch", "allow", flag="DEVKIT_ALLOW_PUSH",
                     rule="hatch")
        audit.record("scope.write", "a-seraph", "a read-only lane wrote files", "recorded",
                     paths=["lib/a.py", "lib/b.py"], certainty="likely")
        audit.record("run.orphaned", "Release", "its session ended", "orphaned")
        gate, hatch, write, run = (audit.query(action=a)[-1] for a in
                                   ("guard.irreversible", "guard.bypass", "scope.write", "run.orphaned"))
        keys = ("irreversible", "tier", "repo", "setting", "grant", "source")
        self.assertEqual({k: gate["ref"].get(k) for k in keys},
                         {"irreversible": "push", "tier": 2, "repo": "acme/app", "setting": "enforce", "grant": "G3",
                          "source": "E2"})
        self.assertEqual(hatch["evidence"]["flag"], "DEVKIT_ALLOW_PUSH")
        self.assertEqual((write["category"], write["severity"], write["evidence"]),
                         ("security", "notice", {"file": "lib/a.py, lib/b.py", "check": "likely"}))
        self.assertEqual((run["category"], run["severity"]), ("lifecycle", "notice"))

    def test_every_keyword_the_code_passes_is_declared(self):
        found = {}
        for f in sorted((LIB / "devkit").glob("*.py")):
            s = f.read_text()
            # guard.py writes through its own wrapper, _record(action, …, **extra); fs.py's _record is another thing
            wrapper = r"|(?<!def )\b_record" if f.name == "guard.py" else ""
            for m in re.finditer(r"(?:audit\.record|\b_audit" + wrapper + r")\(", s):
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
        # cli.py records `dev crew …` with its arguments, one word each (26.11.5)
        cases = [
            (["ask", "--to", "Persephone", "please confirm the Acme roadmap"], "ask --to Persephone"),
            (["answer", "M12", "yes, the roadmap for Acme is fine"], "answer M12"),
            # 26.11.5: each text is replaced where it stands, so the option names stay (compliance review, change 1)
            (["ask", "--decision", "which roadmap for Acme?", "--options", "A: ship | B: wait", "--recommend", "A",
              "--irreversible", "push"], "ask --decision [message text not recorded: 23 characters] --options"),
            (["--json", "ask", "--to=neo", "the roadmap"], "--json ask --to=neo"),
            (["delegate", "the Acme roadmap", "--to", "Tank"], "delegate"),  # what the member should do is free text
        ]
        for argv, kept in cases:
            with self.subTest(argv=argv[:2]):
                audit.record("dev.crew", argv, "", "ok")
                got = audit.query(action="dev.crew")[-1]["target"]
                self.assertTrue(got.startswith(kept + " "), got)
                self.assertNotIn("roadmap", got)
                self.assertIn("characters", got)
        for target in ("mode auto --approve push", "decline 3 --why"):
            with self.subTest(target=target):
                audit.record("dev.crew", target.split(), "", "ok")
                self.assertEqual(audit.query(action="dev.crew")[-1]["target"], target)

    def test_dev_crew_decide_keeps_the_decision_id_but_not_the_answer(self):
        # the options are unknown here: crew.decided keeps the answer, checked against them (M304)
        cases = {"decide D3 after the Acme call, ping Ana": "decide D3 [free text: 29 characters]",
                 "--json decide D3 B: reject": "--json decide D3 [free text: 9 characters]",
                 "decide D3 " + "x" * 290: "decide D3 [free text: 290+ characters]",  # cli.py cuts at 300
                 "decide D3": "decide D3"}
        for target, kept in cases.items():
            with self.subTest(target=target[:24]):
                audit.record("dev.crew", target, "", "ok")
                self.assertEqual(audit.query(action="dev.crew")[-1]["target"], kept)
        self.assertNotIn("Acme", (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text())

    def test_developer_evidence_keeps_the_lines_length_and_an_answer_only_when_it_is_an_option(self):
        # compliance review 2, section 3: accountability needs who approved what, when and how; not the words
        opts = ["A: ship", "B: wait for Acme"]
        cases = [  # (covers, the line or the question tool's excerpt, answer, what ref.answer keeps)
            ("D3", "approve D3: ship it Friday, Acme agreed", "ship it Friday, Acme agreed",
             "[free text: 27 characters]"),
            ("D4", "approve D4: b:  WAIT for acme", "b:  WAIT for acme", "B: wait for Acme"),
            ("D5", "D5 · Ship the Acme roadmap now? = Other: after the Acme call", "Other: after the Acme call",
             "[free text: 26 characters]"),
            ("push feat/x", "grant push feat/x", "", None),
        ]
        for covers, line, answer, kept in cases:
            with self.subTest(covers=covers):
                audit.record("crew.evidence", covers, line, channel="E2", transcript_uuid="prompt-9",
                             session="claude:s1", answer=answer, options=opts)
                r = audit.query(action="crew.evidence")[-1]
                # the length only: a hash of a short option is guessable (The Keymaker, M287)
                self.assertEqual((r["target"], r["detail"]),
                                 (covers, f"[approval line not recorded: {len(line)} characters]"))
                self.assertEqual(r["ref"].get("answer"), kept)
                self.assertEqual((r["evidence"]["channel"], r["evidence"]["transcript_uuid"]), ("E2", "prompt-9"))
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        for words in ("Friday", "roadmap", "Acme call", "A: ship", "options"):
            self.assertNotIn(words, raw)
        # without the decision's options, no answer is kept as said
        audit.record("crew.evidence", "D6", "approve D6: A: ship", answer="A: ship")
        self.assertEqual(audit.query(action="crew.evidence")[-1]["ref"]["answer"], "[free text: 7 characters]")

    def test_a_decision_keeps_its_id_who_asked_its_options_and_the_questions_length(self):
        # progress records a member's question this way; the question quotes the developer's request, the option
        # labels are the member's own and are redacted as usual
        question = "Ana wants the Acme roadmap by Friday: ship or wait?"
        opts = ["A: ship", "B: wait for Acme", f"C: ship with token={VALUE}"]
        audit.record("crew.decision", "D3", question, "pending", project="acme/app", by="", answer="",
                     irreversible="push", mode="guided", asked_by="Tank", options=opts)
        r = audit.query(action="crew.decision")[-1]
        self.assertEqual((r["target"], r["detail"]), ("D3", "[question not recorded: 51 characters]"))
        self.assertEqual(r["ref"], {"asked_by": "Tank", "irreversible": "push", "mode": "guided", "result": "pending",
                                    "options": "A: ship | B: wait for Acme | C: ship with token=" + audit.MARK})
        # an automatic decision keeps the recommendation when it names an option, as `--recommend A` does
        audit.record("crew.decision", "D4", "y" * 300, "decided", by="auto", answer="A", asked_by="Tank", options=opts)
        r = audit.query(action="crew.decision")[-1]
        self.assertEqual((r["detail"], r["ref"]["answer"], r["ref"]["by"]),
                         ("[question not recorded: 300+ characters]", "A: ship", "auto"))
        audit.record("crew.decision", "D5", "ship?", "decided", by="auto", answer="when Ana is back", asked_by="Tank")
        self.assertEqual(audit.query(action="crew.decision")[-1]["ref"]["answer"], "[free text: 16 characters]")
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        for words in ("roadmap", "Friday", "Ana", VALUE):
            self.assertNotIn(words, raw)

    def test_the_developers_answer_stays_only_when_it_names_one_of_the_options(self):
        # progress records the answer this way. An option is named as progress.option_of reads it: its label in any
        # case or spacing, with the question tool's "(Recommended)", or its letter
        opts = ["A: ship", "B: wait for Acme"]
        cases = [("b:  WAIT for acme", "B: wait for Acme"), ("A: ship (Recommended)", "A: ship"),
                 ("B", "B: wait for Acme"), ("after the Acme call, ping Ana", "[free text: 29 characters]")]
        for answer, kept in cases:
            with self.subTest(answer=answer):
                audit.record("crew.decided", "D3", answer, project="acme/app", by="developer", overrides="a",
                             verified=True, channel="E1", transcript_uuid="tool-4", session="claude:s1", options=opts)
                r = audit.query(action="crew.decided")[-1]
                self.assertEqual((r["target"], r["detail"], r["ref"]["by"]), ("D3", kept, "developer"))
                self.assertEqual((r["ref"]["overrides"], r["ref"]["options"]),
                                 ("A: ship", "A: ship | B: wait for Acme"))
        # Morpheus's answer to an irreversible decision, refused; an override of free text; no options at all
        audit.record("crew.decided", "D5", "push it, Ana said so on the Acme call", "blocked", by="morpheus",
                     verified=False, irreversible="push", options=opts)
        self.assertEqual(audit.query(action="crew.decided")[-1]["detail"], "[free text: 37 characters]")
        audit.record("crew.decided", "D6", "B", by="developer", overrides="when Ana is back", options=opts)
        self.assertEqual(audit.query(action="crew.decided")[-1]["ref"]["overrides"], "[free text: 16 characters]")
        audit.record("crew.decided", "D7", "A: ship", by="morpheus")
        self.assertEqual(audit.query(action="crew.decided")[-1]["detail"], "[free text: 7 characters]")
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        for words in ("Ana", "Acme call"):
            self.assertNotIn(words, raw)

    def test_records_sealed_under_older_rules_are_cleaned_on_read_and_still_verify(self):
        # compliance review 2, section 3: 285 v2 records sealed before f5c49a6 hold message text or a home path, and
        # lift returned them as stored. Seal records the way that code did, beside one the current code writes.
        old = [("crew.message", "Neo → Persephone", "Ana wants the Acme roadmap by Friday", {"ref": {"id": "M3"}}),
               ("dev.crew", "ask --to persephone is the Acme roadmap fine", "", {}),
               ("dev.rm", "/Users/fixture-user/Development/acme/build", f"{CURL} -u admin:{CMD} https://x.example",
                {"evidence": {"cwd": "/Users/fixture-user/Development"}}),
               ("crew.evidence", "D2", "approve D2: after the Acme roadmap call", {"ref": {"answer": "after the "
                                                                                          "Acme roadmap call"}}),
               ("crew.evidence", "D7", "[approval line not recorded: 19 characters, sha256 " + "ab" * 32 + "]",
                {"ref": {"answer": "B: wait for Acme"}})]  # as 6677b15 wrote it
        for action, target, detail, more in old:
            ts = round(time.time(), 3)
            audit._append({"v": 2, "id": audit.new_id(ts), "ts": ts, "time": audit.iso(ts),
                           "actor": {"user": "fixture-user", "host": "fixture-host", "agent": "claude", "session": ""},
                           "action": action, "category": "change", "severity": "info", "target": target,
                           "detail": detail, "outcome": "ok", **more})
        audit.record("crew.message", "Neo → Morpheus", "the Acme roadmap again", id="M4")
        audit.record("dev.crew", "answer M4 the Acme roadmap is fine", "", "ok")
        stored = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_bytes()
        recs = audit.query(days=1)
        for fmt in audit.FORMATS:
            with self.subTest(format=fmt):
                out = audit.render(fmt, recs)
                for words in ("roadmap", "/Users/fixture-user", CMD):
                    self.assertNotIn(words, out)
        sealed = [r for r in recs if r["actor"]["user"] == "fixture-user"]
        self.assertEqual([r.get("cleaned") for r in sealed], [True] * 5)
        self.assertEqual(sealed[3]["ref"], {"answer": "[free text: 27 characters]"})  # never checked against options
        self.assertEqual((sealed[4]["detail"], sealed[4]["ref"]),
                         ("[approval line not recorded: 19 characters]", {"answer": "B: wait for Acme"}))
        self.assertEqual((sealed[0]["detail"], sealed[0]["ref"]), ("[message text not recorded: 36 characters]",
                                                                   {"id": "M3"}))
        # joined by spaces before 26.11.5, a word cannot tell where the member's name ends and the message starts
        self.assertEqual(sealed[1]["target"], "ask --to [message text not recorded: 35 characters]")
        self.assertEqual((sealed[2]["target"], sealed[2]["evidence"]), ("~/Development/acme/build",
                                                                        {"cwd": "~/Development"}))
        new = [r for r in recs if r["actor"]["user"] != "fixture-user" and r["action"] != "audit.config"]
        stored_new = [json.loads(ln) for ln in stored.splitlines()][-2:]
        self.assertEqual(new, stored_new)  # a record the current rules wrote reads back as stored, unmarked
        self.assertEqual((audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_bytes(), stored)
        self.assertTrue(audit.verify(team=False)["ok"])  # the chain still covers the stored bytes

    def test_decision_records_sealed_before_the_rule_are_cleaned_on_read_and_still_verify(self):
        # until now crew.decision held the question and crew.decided the answer as typed, in v1 and in v2 records
        month = audit.AUDIT / time.strftime("%Y-%m.jsonl")
        audit.AUDIT.mkdir(parents=True)
        month.write_text(json.dumps({"ts": time.time() - 60, "user": "fixture-user", "host": "mac", "agent": "claude",
                                     "session": "", "action": "crew.decided", "target": "D1",
                                     "detail": "after the Acme roadmap call", "outcome": "ok", "by": "developer",
                                     "overrides": "ship it, Ana"}) + "\n")
        old = [("crew.decision", "D2", "Ana wants the Acme roadmap: ship or wait?",
                {"ref": {"asked_by": "Tank", "by": "auto", "answer": "wait for the Acme call"}}),
               ("crew.decided", "D2", "after the Acme roadmap call", {"ref": {"by": "developer", "overrides": "A"}}),
               ("dev.crew", "decide D2 after the Acme roadmap call", "", {})]
        for action, target, detail, more in old:
            ts = round(time.time(), 3)
            audit._append({"v": 2, "id": audit.new_id(ts), "ts": ts, "time": audit.iso(ts),
                           "actor": {"user": "fixture-user", "host": "fixture-host", "agent": "claude", "session": ""},
                           "action": action, "category": "decision", "severity": "notice", "target": target,
                           "detail": detail, "outcome": "ok", **more})
        opts = ["A: ship", "B"]
        audit.record("crew.decision", "D8", "the Acme roadmap again", "pending", asked_by="Tank", options=opts)
        audit.record("crew.decided", "D8", "a", by="developer", overrides="the Acme roadmap", options=opts)
        audit.record("dev.crew", "decide D8 the Acme roadmap", "", "ok")
        stored = month.read_bytes()
        recs = [r for r in audit.query(days=1) if r["action"] != "audit.config"]
        for fmt in audit.FORMATS:
            with self.subTest(format=fmt):
                self.assertFalse("roadmap" in audit.render(fmt, recs), f"the {fmt} export shows a question or answer")
        v1, decision, decided, decide = recs[:4]
        self.assertEqual((v1["v"], v1["detail"], v1["ref"]),
                         (1, "[free text: 27 characters]", {"by": "developer", "overrides": "[free text: 12 characters]"}))
        self.assertEqual((decision["detail"], decision["ref"]), ("[question not recorded: 41 characters]", {
            "asked_by": "Tank", "by": "auto", "answer": "[free text: 22 characters]"}))
        # sealed without its options, the answer is never checked against them
        self.assertEqual((decided["detail"], decided["ref"]["overrides"]), ("[free text: 27 characters]",
                                                                            "[free text: 1 characters]"))
        self.assertEqual(decide["target"], "decide D2 [free text: 27 characters]")
        self.assertEqual([r.get("cleaned") for r in recs[1:4]], [True] * 3)
        again = {k: v for k, v in decision.items() if k != "cleaned"}
        self.assertEqual(audit.lift(again), again)  # a marker read again stays as it is
        new = recs[4:]
        self.assertEqual(new, [json.loads(ln) for ln in stored.splitlines()][-3:])  # read back as stored, unmarked
        self.assertEqual((new[0]["detail"], new[1]["detail"], new[1]["ref"]["overrides"]),
                         ("[question not recorded: 22 characters]", "A: ship", "[free text: 16 characters]"))
        self.assertEqual(month.read_bytes(), stored)
        self.assertTrue(audit.verify(team=False)["ok"])  # the chain still covers the stored bytes

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


class CommandTextTests(AuditCase):
    # the compliance review of 2026-11-07, change 1: under D6 = A the records go to the handoff repository for 13
    # months and are never rewritten. A record that keeps a command line keeps only the words known to be safe (the
    # program and its subcommands, option names, the values that name something, ids and numbers, operators) and
    # each other word as its length: the Code reviewer's second review found five ways around a masker that looked
    # for the free text, so it now keeps the safe words instead (L-fz3qnrebvm5). Tests carry comments, not docstrings.
    def stored(self) -> str:
        return (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()

    def t(self, n) -> str:
        return f"[text not recorded: {n} characters]"

    def m(self, n) -> str:
        return f"[message text not recorded: {n} characters]"

    def denied(self, cmd: str, **extra) -> str:
        audit.record("guard.deny", cmd, "refused for the test", "deny", **extra)
        return audit.query(action="guard.deny")[-1]["target"]

    def unmarked(self):
        # each record the current rules wrote reads back as it was stored
        recs = [r for r in audit.query(days=1) if r["action"] != "audit.config"]
        self.assertEqual([r for r in recs if r.get("cleaned")], [])

    def test_dev_task_add_keeps_its_options_but_not_the_title_or_the_reason(self):
        argv = shlex.split('task add "Fix the Acme invoice export" --why "Ana asked on Friday" --type bug')
        audit.record("dev." + argv[0], argv[1:], "", "ok")  # cli.py passes dev's arguments as a list
        self.assertEqual(audit.query(action="dev.task")[-1]["target"],
                         f"add {self.t(27)} --why {self.t(19)} --type bug")
        for words in ("Acme", "Ana", "Friday"):
            self.assertNotIn(words, self.stored())

    def test_each_argument_of_a_dev_command_is_one_word_so_a_text_option_takes_exactly_one(self):
        # M3: cli.py joined dev's arguments with spaces, so a text holding " -n " or " --to " read as an option and the
        # words after it stayed; the masker now gets the list, so an option's value is one argument, whatever it holds
        cases = [
            ("task", ["done", "3", "--why", "ran it with -n to check SECRET"], f"done 3 --why {self.t(30)}"),
            ("task", ["done", "3", "--why", "moved --to SECRET"], f"done 3 --why {self.t(17)}"),
            ("task", ["done", "3", "--why", "-e SECRET"], f"done 3 --why {self.t(9)}"),
            ("task", ["add", "-e SECRET plan"], f"add {self.t(14)}"),
            ("task", ["add", "--", "-secret SECRET"], f"add -- {self.t(14)}"),
            ("task", ["done", "3", "--why", f"{self.t(1)} SECRET"], f"done 3 --why {self.t(40)}"),
            ("crew", ["set", "tank", "mission", "SECRET mission", "--why", "x"],
             f"set tank mission {self.t(14)} --why {self.t(1)}"),
            ("crew", ["ask", "--to", "The Keymaker", "SECRET"], f"ask --to {self.m(12)} {self.m(6)}"),
            ("note", ["set", "milestone=SECRET launch", "status=blocked"], f"set milestone={self.t(13)} status={self.t(7)}"),
            ("progress", ["step", "done", "--check", "SECRET", "-p", "x"], f"step {self.t(4)} --check {self.t(6)} -p x"),
        ]
        for cmd, argv, kept in cases:
            with self.subTest(argv=argv[:3]):
                audit.record("dev." + cmd, argv, "", "ok")
                self.assertEqual(audit.query(action="dev." + cmd)[-1]["target"], kept)
        self.unmarked()
        self.assertNotIn("SECRET", self.stored())

    def test_the_cli_records_a_dev_command_from_its_argument_list(self):
        # M3, through cli.main: dev crew decline fails here (no run), and every audited dev command is recorded anyway
        from devkit import cli
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["crew", "decline", "3", "--why", "ran it with -n to check SECRET"])
        self.assertEqual(audit.query(action="dev.crew")[-1]["target"], f"decline 3 --why {self.t(30)}")
        self.assertNotIn("SECRET", self.stored())

    def test_a_text_joined_before_26_11_5_keeps_only_the_path_option_names_and_ids(self):
        # records sealed by cli.py before 26.11.5 hold the arguments joined by spaces, where a word cannot tell where
        # an argument ends: once a word there is free text, only the command, option names, markers and ids stay
        cases = {("task", "done 3 --why ran it with -n to check SECRET"): f"done 3 --why {self.t(11)} -n {self.t(15)}",
                 ("task", "add -e SECRET plan"): f"add -e {self.t(11)}",
                 ("task", "done SECRET Acme export --why x"): f"done {self.t(18)} --why {self.t(1)}",
                 ("crew", "ask --to Persephone [message text not recorded: 44 characters]"):
                     "ask --to Persephone [message text not recorded: 44 characters]"}  # a marker: read as stored
        for (cmd, joined), kept in cases.items():
            with self.subTest(joined=joined[:16]):
                self.assertEqual(audit.mask_command(joined, "argv", ("dev", cmd), 300), kept)

    def test_a_selector_stays_only_when_it_is_an_id_or_a_number(self):
        # M2: dev task done "Acme export" selects a task by a part of its title
        cases = [("task", ["done", "3"], "done 3"), ("task", ["done", "T-fwk7a2abc"], "done T-fwk7a2abc"),
                 ("task", ["done", "SECRET", "Acme", "export"], f"done {self.t(18)}"),
                 ("lesson", ["recur", "L-fz3qnrebvm5", "--evidence", "SECRET"], f"recur L-fz3qnrebvm5 --evidence {self.t(6)}"),
                 ("crew", ["answer", "M12", "yes"], f"answer M12 {self.m(3)}"),
                 ("crew", ["decline", "SECRET step", "--why", "x"], f"decline {self.t(11)} --why {self.t(1)}"),
                 ("progress", ["assign", "2", "--to", "Tank", "--scope", "lib/**"], "assign 2 --to Tank --scope lib/**")]
        for cmd, argv, kept in cases:
            with self.subTest(argv=argv[:2]):
                audit.record("dev." + cmd, argv, "", "ok")
                self.assertEqual(audit.query(action="dev." + cmd)[-1]["target"], kept)
        self.assertNotIn("SECRET", self.stored())

    def test_a_shell_command_keeps_its_options_and_ids_but_not_its_free_text(self):
        # what the hooks keep of a refused call: the shell text, quotes, operators and redirections included
        cases = {
            'dev crew answer M12 "yes, ship the Acme roadmap"': f"dev crew answer M12 {self.m(26)}",
            'dev crew decide D3 "after the Acme call"': "dev crew decide D3 [free text: 19 characters]",
            'dev progress step "Acme parser done" --check="12 tests pass" --next "review"':
                f"dev progress step {self.t(16)} --check={self.t(13)} --next {self.t(6)}",
            'cd ~/x && dev handoff --log "shipped; next: Acme rooms" > /dev/null 2>&1':
                f"{self.t(2)} {self.t(3)} && dev handoff --log {self.t(25)} > /dev/null 2>&1",
            "git commit -am \"fix: keep Acme's totals\" && git push": f"git commit -am {self.t(23)} && git push",
            "gh pr create --title \"Acme export\" --body \"Ana's notes\" --base main":
                f"gh pr create --title {self.t(11)} --body {self.t(11)} --base main",
            'git commit -m "-WIP SECRET"': f"git commit -m {self.t(11)}",  # L2: a value that starts with -
            'dev task add "-e SECRET plan"': f"dev task add {self.t(14)}",
            'dev task add -- "-secret SECRET"': f"dev task add -- {self.t(14)}",
            "gh api repos/a/b/issues -f body='SECRET'": f"gh api {self.t(16)} -f {self.t(11)}",
            'dev crew set tank mission "SECRET mission" --why x': f"dev crew set tank mission {self.t(14)} --why {self.t(1)}",
            'dev note set milestone="SECRET launch"': f"dev note set milestone={self.t(13)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("Acme", "Ana", "roadmap", "shipped", "SECRET"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_shell_line_finds_its_program_behind_reserved_words_wrappers_and_nested_shells(self):
        # H2: the Code reviewer's forms; the program is found and its free text masked, and the words around it that
        # are not known to be safe become their length
        t, m = self.t, self.m
        cases = {
            '{ dev crew ask "SECRET plan"; }': f"{{ dev crew ask {m(11)}; }}",
            'if true; then dev crew ask "SECRET plan"; fi': f"if {t(4)}; then dev crew ask {m(11)}; fi",
            'for x in a; do dev task add "SECRET plan"; done':
                f"{t(3)} {t(1)} {t(2)} {t(1)}; do dev task add {t(11)}; done",
            '! dev crew ask "SECRET plan"': f"! dev crew ask {m(11)}",
            'timeout 60 dev crew ask "SECRET plan"': f"timeout 60 dev crew ask {m(11)}",
            'echo 1 | xargs dev crew ask "SECRET plan"': f"{t(4)} {t(1)} | xargs dev crew ask {m(11)}",
            'bash -c \'dev crew ask "SECRET plan"\'': f"bash -c 'dev crew ask {m(11)}'",
            "eval 'dev crew ask SECRET'": f"eval 'dev crew ask {m(6)}'",
            'zsh -lc "git commit -m \\"SECRET fix\\" && git push"': f'zsh -lc "git commit -m {t(10)} && git push"',
            'sudo -u root dev crew ask "SECRET"': f"sudo -u {t(4)} dev crew ask {m(6)}",
            'nice -n 5 dev crew ask "SECRET"': f"nice -n 5 dev crew ask {m(6)}",
            'uv run dev crew ask "SECRET"': f"uv run dev crew ask {m(6)}",
            'python3 -I -m devkit crew ask "SECRET"': f"python3 -I -m devkit crew ask {m(6)}",
            '~/Development/x/bin/dev crew ask "SECRET"': f"~/Development/x/bin/dev crew ask {m(6)}",
            'dev crew ask <<< "SECRET"': f"dev crew ask <<< {t(6)}",
            'time -p dev --json crew ask "SECRET"': f"time -p dev --json crew ask {m(6)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        self.denied("dev task done 3 --why $'Ana\\'s SECRET review'")  # L1's form: an escaped quote in $'…'
        self.assertNotIn("SECRET", self.stored())
        self.unmarked()

    def test_a_line_without_a_known_program_keeps_only_its_operators_and_each_words_length(self):
        t = self.t
        cases = {
            "ls -la /tmp # SECRET notes": f"{t(2)} {t(3)} {t(4)} # {t(12)}",
            "rm -rf src && python3 -m unittest tests.test_audit > out.log 2>&1":
                f"{t(2)} {t(3)} {t(3)} && {t(7)} {t(2)} {t(8)} {t(16)} > {t(7)} 2>&1",
            "cat > notes.md <<'EOF'\nAna's salary review\nEOF": f"{t(3)} > {t(8)} <<'EOF'\n{t(19)}\nEOF",
            "cat <<\\EOF\nSECRET line\nEOF": f"{t(3)} <<\\EOF\n{t(11)}\nEOF",  # L3: a heredoc whose delimiter is escaped
            "echo SECRET | tee -a notes.md >/dev/null": f"{t(4)} {t(6)} | {t(3)} {t(2)} {t(8)} >/dev/null",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("SECRET", "Ana", "unittest", "notes.md"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_an_assignment_keeps_its_name_and_only_a_short_devkit_setting_keeps_its_value(self):
        # M4: a variable can carry the text the command then uses ("$MSG")
        t, m = self.t, self.m
        cases = {'MSG="SECRET plan"; dev crew ask "$MSG"': f"MSG={t(11)}; dev crew ask {m(4)}",
                 'MSG="SECRET plan" dev crew ask "$MSG"': f"MSG={t(11)} dev crew ask {m(4)}",
                 'DEVKIT_GUARD=off DEVKIT_X="1" dev sync': 'DEVKIT_GUARD=off DEVKIT_X="1" dev sync',
                 'DEVKIT_NOTE="SECRET plan" dev sync': f"DEVKIT_NOTE={t(11)} dev sync",
                 "exec env A=SECRET dev sync": f"exec env A={t(6)} dev sync"}
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        self.assertNotIn("SECRET", self.stored())

    def test_a_guard_refusal_records_the_commands_it_quotes_masked(self):
        # H1: guard.deny's detail is the refusal the agent reads, and it quotes the irreversible command's words in
        # backticks; the record masks each quoted command, and the agent still reads it whole
        from devkit import guard
        words = "gh pr merge 12 --squash --body SECRET release notes for Acme"
        reason = guard._main_reason({"action": "merge", "tier": 2, "target": "#12", "repo": "acme/app", "words": words})
        self.assertIn(words, reason)
        audit.record("guard.deny", 'gh pr merge 12 --squash --body "SECRET release notes for Acme"', reason[:400],
                     "deny", tool="Bash")
        detail = audit.query(action="guard.deny")[-1]["detail"]
        self.assertTrue(detail.startswith(f"`gh pr merge 12 --squash --body {self.t(29)}` merges PR #12"), detail)
        # an irreversible command the guard cannot parse: its words are the shell text the agent ran
        audit.record("guard.irreversible", "?", "cat <<'EOF' | gh pr merge 12 --body-file -\nSECRET notes\nEOF", "deny",
                     irreversible="unparsed", tier=2)
        self.assertEqual(audit.query(action="guard.irreversible")[-1]["detail"],
                         f"{self.t(3)} <<'EOF' | gh pr merge 12 --body-file -\n{self.t(12)}\nEOF")
        # a record that does not say what it holds is masked as words; only the gate's own errors are kept as they are
        audit.record("guard.irreversible", "#12", "gh pr merge 12 --body SECRET notes", "deny")
        audit.record("guard.irreversible", "", "RuntimeError: state busy", "deny", rule="gate-error")
        self.assertEqual([r["detail"] for r in audit.query(action="guard.irreversible")[-2:]],
                         [f"gh pr merge 12 --body {self.t(12)}", "RuntimeError: state busy"])
        for text in ("SECRET", "Acme"):
            self.assertNotIn(text, self.stored())
        self.unmarked()

    def test_a_delegated_task_and_a_handoff_commit_keep_no_text(self):
        # M1: crew.delegate keeps what a member should do only as its length, and a handoff commit's record keeps the
        # item's id and the operation, not its title or reason
        audit.record("crew.delegate", "Morpheus → Tank", "SECRET: fix the Acme invoice export", "ok", step="api")
        audit.record("handoff.tasks", "tasks/acme/app/T-1.md", "T-1: create task “SECRET Acme export” (me) — Ana asked")
        audit.record("handoff.tasks", "tasks/acme/app/T-fwk7a2abc.md", "T-fwk7a2abc: done (ana@mac)")
        audit.record("handoff.sessions", "sessions/acme/app.md", "session log: acme/app (ana@mac)")
        recs = [r for r in audit.query(days=1) if r["action"] != "audit.config"]
        self.assertEqual([(r["target"], r["detail"]) for r in recs], [
            ("Morpheus → Tank", self.t(35)), ("tasks/acme/app/T-1.md", f"T-1: create {self.t(42)}"),
            ("tasks/acme/app/T-fwk7a2abc.md", f"T-fwk7a2abc: done {self.t(9)}"), ("sessions/acme/app.md", self.t(31))])
        for text in ("SECRET", "Ana", "ana@mac"):
            self.assertNotIn(text, self.stored())
        self.unmarked()

    def test_a_heredoc_body_is_free_text(self):
        cases = {"git commit -m \"$(cat <<'EOF'\nfix: Acme's totals\n\nIt's Ana's fix\nEOF\n)\"":
                 f"git commit -m {self.t(54)}"}
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:12]):
                self.assertEqual(self.denied(cmd), kept)
        self.assertNotIn("Ana", self.stored())

    def test_a_command_cut_inside_its_text_records_a_lower_bound(self):
        # the hooks cut a refused command at 200 characters, here inside the quoted message
        audit.record("guard.deny", ('dev crew ask "' + "x" * 190 + '"')[:200], "", "deny")
        self.assertEqual(audit.query(action="guard.deny")[-1]["target"],
                         "dev crew ask [message text not recorded: 186+ characters]")

    def test_the_evidence_command_is_masked_like_the_target(self):
        audit.record("guard.deny", "Bash", "", "deny", command='dev crew ask "a secret plan for acme"')
        self.assertEqual(audit.query(action="guard.deny")[-1]["evidence"]["command"],
                         "dev crew ask [message text not recorded: 22 characters]")
        self.assertNotIn("secret plan", self.stored())

    def test_a_record_of_safe_words_is_kept_as_it_is(self):
        cases = [("guard.deny", "gh pr merge 12 --squash", "", {}),
                 ("guard.deny", "dev task done T-fwk7a2abc --json && git push --force-with-lease", "", {}),
                 ("guard.ask", "git -C ~/x push --force-with-lease", "", {"tool": "Bash"}),
                 ("guard.deny", "~/Development/acme/app/notes.md", "", {"tool": "Write"}),  # a file tool: its path
                 ("guard.deny", "mcp__claude_ai_Gmail__send", "", {"tool": "mcp__claude_ai_Gmail__send"}),
                 ("guard.irreversible", "#12", "gh pr merge 12 --squash", {}),  # its target is no command line
                 ("calls.unclosed", "main", "dev sync --quiet (no result within 120 s)", {}),
                 ("dev.wt", ["new", "feat/invoice-export"], "", {}),
                 ("dev.task", ["done", "3"], "", {}),
                 ("job.sync", "sync --scheduled --quiet", "", {})]
        for action, target, detail, extra in cases:
            audit.record(action, target, detail, "ok", **extra)
        stored = [json.loads(ln) for ln in self.stored().splitlines() if '"audit.config"' not in ln]
        self.assertEqual([(r["action"], r["target"], r["detail"]) for r in stored],
                         [(a, " ".join(t) if isinstance(t, list) else t, d) for a, t, d, _ in cases])
        read = [r for r in audit.query(days=1) if r["action"] != "audit.config"]
        self.assertEqual(read, stored)  # read back as stored, not marked cleaned

    def test_a_record_sealed_before_the_rule_is_masked_on_read_and_still_verifies(self):
        # records written by 26.11.3 and 26.11.4 hold the text; the current rules write a record that reads back as
        # it was stored
        old = [("guard.deny", 'DEVKIT_X=1 dev crew ask "a secret plan for acme"', "refused"),
               ("guard.deny", "rm -rf src", "refused"),
               ("calls.unclosed", "main", 'dev crew ask "a secret plan for acme" (no result within 120 s)'),
               ("dev.task", "add a secret plan for acme --why Ana asked", "")]
        for action, target, detail in old:
            ts = round(time.time(), 3)
            audit._append({"v": 2, "id": audit.new_id(ts), "ts": ts, "time": audit.iso(ts),
                           "actor": {"user": "fixture-user", "host": "fixture-host", "agent": "claude", "session": ""},
                           "action": action, "category": "change", "severity": "info", "target": target,
                           "detail": detail, "outcome": "ok"})
        for action, target, detail in old:
            audit.record(action, target, detail, "deny")
        recs = [r for r in audit.query(days=1) if r["action"] != "audit.config"]
        sealed, new = recs[:4], recs[4:]
        self.assertEqual([r.get("cleaned") for r in sealed], [True] * 4)
        self.assertEqual([(r["target"], r["detail"]) for r in sealed],
                         [("DEVKIT_X=1 dev crew ask [message text not recorded: 22 characters]", "refused"),
                          (f"{self.t(2)} {self.t(3)} {self.t(3)}", "refused"),
                          ("main", "dev crew ask [message text not recorded: 22 characters] (no result within 120 s)"),
                          (f"add {self.t(22)} --why {self.t(9)}", "")])
        self.assertEqual([(r["target"], r["detail"]) for r in new], [(r["target"], r["detail"]) for r in sealed])
        stored_new = [json.loads(ln) for ln in self.stored().splitlines()][-4:]
        self.assertEqual(new, stored_new)  # a record the current rules wrote reads back as stored, unmarked
        self.assertNotIn("secret plan", self.stored().split("\n", 4)[-1])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_every_dev_command_positional_and_valued_option_is_classified(self):
        # M2: the tables follow dev's parser. Every command is listed with each positional, in the parser's order,
        # with its nargs and a class (text, name, select or pair); every option that takes a value is free text or
        # names something; and a NAME option a command reads as a flag is listed for that command. A new command,
        # positional or valued option fails here until it is classified. Whether a class is right is not checked
        # here: the masking tests above check the classes that hold text
        import argparse
        from devkit import cli
        valued, commands, flags = set(), {}, {}

        def walk(p, path):
            here = []
            for act in p._actions:
                if isinstance(act, argparse._SubParsersAction):
                    for name, sub in act.choices.items():
                        walk(sub, path + [name])
                elif not act.option_strings:
                    here.append((act.dest, {None: "", argparse.REMAINDER: "..."}.get(act.nargs, act.nargs)))
                elif not isinstance(act, argparse._HelpAction):
                    if act.nargs != 0:
                        valued.update(act.option_strings)
                    if act.nargs in (0, "?", "*"):
                        flags.setdefault(" ".join(path), set()).update(o for o in act.option_strings
                                                                       if o in audit.NAME_OPTIONS)
            commands[" ".join(path)] = here
        walk(cli.build_parser(), [])
        self.assertEqual(sorted(valued - audit.TEXT_OPTIONS - audit.NAME_OPTIONS), [])
        self.assertEqual(sorted(audit.TEXT_OPTIONS & audit.NAME_OPTIONS), [])
        self.assertEqual(sorted(set(commands) ^ set(audit.COMMAND_ARGS)), [])
        for path, positionals in commands.items():
            with self.subTest(command=path):
                slots = audit.command_slots(path)
                self.assertEqual([(dest, nargs) for dest, _, nargs in slots], positionals)
                self.assertEqual([c for _, c, _ in slots if c not in audit.ARG_CLASSES], [])
        self.assertEqual({k: v for k, v in flags.items() if v}, {k: set(v) for k, v in audit.NAME_FLAGS.items()})


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
        # free text that reaches an export: a formula in the reason; a command line the masker keeps whole, with a pipe
        audit.record("dev.task", ["done", "3"], 'a "quoted", comma\nand a new line', "ok", why="=HYPERLINK(\"http://x\")")
        audit.record("guard.deny", "dev sync | dev status", "key=value \\ back", "deny", tool="Bash")

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
        self.assertEqual(rows[1][col("why")], "'=HYPERLINK(\"http://x\")")  # no formula runs in the sheet

    def test_cef_lines_escape_their_separators(self):
        lines = audit.export("cef", action="guard").splitlines()
        self.assertEqual(len(lines), 1)
        head = re.split(r"(?<!\\)\|", lines[0])
        self.assertEqual(head[:4], ["CEF:0", "Firefly Software Solutions", "devkit", core.VERSION])
        self.assertEqual((head[4], head[6]), ("guard.deny", "6"))
        self.assertIn(r"dev sync \| dev status", lines[0])
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
        audit.record("dev.task", "done 7")
        self.assertEqual(json.loads(sink.read_text().splitlines()[-1])["target"], "done 7")
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv.settimeout(5)
        core.CFG["audit"]["forward"] = f"syslog://127.0.0.1:{srv.getsockname()[1]}"
        audit.record("dev.task", "done 8")
        data = ""
        while "done 8" not in data:  # the settings change is forwarded first
            data = srv.recv(65536).decode()
        srv.close()
        self.assertIn("CEF:0|Firefly Software Solutions|devkit|", data)


class QueryTests(AuditCase):
    # R6: filters, show, stats and config
    def setUp(self):
        super().setUp()
        audit.record("dev.task", "add x", "", "ok", project="acme/app")
        audit.record("guard.deny", "git push --force", "", "deny", project="acme/web")
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
        self.assertEqual(audit.show(r["id"])["target"], "git push --force")
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
        audit.record("dev.task", "done 1")
        audit.record("dev.task", "done 2")
        self.assertEqual(self.run_cmd("verify")[0], 0)
        f = audit.AUDIT / time.strftime("%Y-%m.jsonl")
        f.write_text(f.read_text().replace("done 1", "done 9"))
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
        audit.record("dev.task", "done 3", "", "ok", project="acme/app")
        rc, text = self.run_cmd("--project", "acme/app", "--format", "csv")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual((rc, len(rows), rows[1][rows[0].index("target")]), (0, 2, "done 3"))
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


class PublishLimitTests(AuditCase):
    # the 26.11.3 stopgap: GitHub refuses a file over 100 MB, and then every handoff push, so the audit job never
    # publishes a month file over 45 MB (26.11.5 replaces whole months with daily gzipped segments)
    MB = 1024 * 1024

    def month(self) -> Path:
        return audit.AUDIT / time.strftime("%Y-%m.jsonl")

    def grow(self, size: int, at: int = 0):
        # the month file at `size` bytes: its first `at` bytes (what is already published), a hole, then the rest of
        # its records, so the file still extends its published copy and no test writes tens of megabytes
        data = self.month().read_bytes()
        with open(self.month(), "wb") as fh:
            fh.write(data[:at])
            fh.seek(size - (len(data) - at) - 1)
            fh.write(b"\n" + data[at:])

    def run_cmd(self, *argv):
        import argparse
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = audit.cmd_audit(a)
        return rc, out.getvalue()

    def test_a_month_over_45_mb_keeps_its_published_copy_and_records_one_warning(self):
        audit.record("dev.task", "add 1")
        self.assertEqual(len(audit.publish()), 1)  # published while small, as on every day before
        published = audit.team_dir() / self.month().name
        before = published.read_bytes()
        audit.record("dev.task", "add 2")
        self.grow(46 * self.MB, at=len(before))
        last = audit.AUDIT / f"{audit.months_back(time.time(), 1)}.jsonl"
        last.write_bytes(b'{"ts": 1, "action": "dev.task", "target": "last month"}\n')
        self.assertEqual(audit.publish(), [audit.team_dir() / last.name])  # the other months publish as before
        self.assertEqual(published.read_bytes(), before)
        warn = audit.query(action="audit.publish")
        self.assertEqual(len(warn), 1)
        self.assertEqual((warn[0]["outcome"], warn[0]["severity"]), ("blocked", "warning"))
        self.assertEqual(warn[0]["target"], core.tilde(self.month()))
        self.assertIn("46.0 MB", warn[0]["detail"])
        self.assertIn("45.0 MB limit", warn[0]["detail"])

    def test_the_limit_is_45_mb_inclusive(self):
        audit.record("dev.task", "add 1")
        self.grow(45 * self.MB)
        published = audit.team_dir() / self.month().name
        self.assertEqual(audit.publish(), [published])
        self.assertEqual(published.read_bytes(), self.month().read_bytes())  # byte for byte, as today
        with open(self.month(), "ab") as fh:
            fh.write(b"\n")
        self.assertEqual(audit.publish(), [])
        self.assertEqual(published.stat().st_size, 45 * self.MB)

    def test_stats_says_which_month_is_held_and_what_to_do(self):
        audit.record("dev.task", "add 1")
        self.assertEqual(json.loads(self.run_cmd("stats", "--json")[1]).get("held"), [])  # while every month fits
        self.grow(46 * self.MB)
        rc, text = self.run_cmd("stats")
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.month().stem} is not published: 46.0 MB here, over the 45.0 MB limit", text)
        self.assertIn("dev update", text)
        held = json.loads(self.run_cmd("stats", "--json")[1])["held"]
        self.assertEqual([(h["month"], h["bytes"]) for h in held], [(self.month().stem, 46 * self.MB)])

    def test_doctor_fails_its_row_for_a_held_month_and_says_what_to_do(self):
        # dev doctor prints each row as (group, name, ok, detail, fix)
        audit.record("dev.task", "add 1")
        self.assertEqual([row[2] for row in audit.publish_checks()], [True])
        self.grow(46 * self.MB)
        rows = audit.publish_checks()
        self.assertEqual([row[2] for row in rows], [False])
        self.assertIn(f"{self.month().stem} is not published: 46.0 MB here, over the 45.0 MB limit", rows[0][3])
        self.assertIn("dev update", rows[0][4])


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
