# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The audit records spec (P-fxs8za703yx): schema v2, the hash chain and `dev audit verify`, retention, level,
# exports, queries and the redactor. One test per acceptance criterion; every test runs in a temporary folder.
import calendar
import contextlib
import csv
import inspect
import io
import itertools
import json
import os
import random
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, jobs, progress  # noqa: E402

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
    # test input: a command whose password the audit trail must mask
    ("sshpass -p", f"sshpass -P assword -p '{CMD}' ssh deploy@host", CMD,  # nosemgrep: generic.secrets.security.detected-ssh-password.detected-ssh-password
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

    def run_ref(self, agent, sid) -> str:
        # how a record names a session's run: r-<its id>, as crew message refs do
        from devkit import messaging
        return "r-" + messaging.run_id(json.loads(progress.run_path(agent, sid).read_text()))

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
        core.CFG["audit"]["redact"] = {"extra": [r"client-[a-z]{5}"]}  # one that does not compile withholds instead
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
        # text that makes each command rule start a scan at every word, at 16 KB, 4 times that and 16 times that: each
        # step takes a linear redactor about 4 times as long, and a rule that scans to the end from every word about
        # 16 times. Such a rule can be cheap next to the rest on short text and grow only on long text, so the
        # growth of each step is checked, not the two ends. A ratio of two CPU times measured the same way holds on
        # a slow or loaded machine, where a time limit does not; the fastest of two tries leaves out a pause the
        # process did not cause
        shapes = [("curl -u ", ""), ("sshpass -P x ", ""), ("mysql -u ", ""), ("docker login -u ", ""),
                  ("Authorization: token ", "a"), ("gh secret set X ", ""), ("passphrase credentials ", ""),
                  ("https://u:", "@"), ("", "a"), ("echo x | sudo ", "")]

        def text(head: str, fill: str, n: int) -> str:
            return head + fill * n if fill else head * (n // len(head))

        def cpu(t: str) -> float:
            start = time.process_time()
            audit.redact(t, extra=[])
            audit.SECRET.search(t)
            return time.process_time() - start

        growth = {}
        for head, fill in shapes:
            times = [max(min(cpu(text(head, fill, n)) for _ in range(2)), 1e-6) for n in (1 << 14, 1 << 16, 1 << 18)]
            growth[head or fill] = [later / before for before, later in zip(times, times[1:])]
        worst = max(growth, key=lambda k: max(growth[k]))
        self.assertLess(max(growth[worst]), 8, f"{worst!r}: each 4 times the text took "
                        f"{' then '.join(f'{g:.1f}' for g in growth[worst])} times as long")

    def test_no_prompt_text_reaches_a_record(self):
        audit.record("canary.miss", "devkit", "final message without the status block", "recorded",
                     prompt="please ship the secret roadmap", text="the secret roadmap")
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        self.assertNotIn("secret roadmap", raw)


class TeamPatternsFailClosedTests(AuditCase):
    # A team policy whose redact.extra patterns cannot load withholds text instead of redacting it with fewer
    # patterns: a token shape only the team's patterns know is an ordinary word to the built-in rules.
    TOKEN = "acmetok-" + "7f3k9x2m4q8zr1"
    PATTERN = r"acmetok-[a-z0-9]{14}"
    TEXT = "the deploy key " + TOKEN + " was refused"
    GITHUB = "ghp_" + A36  # a shape the built-in rules know

    def write_policy(self, body: str):
        f = audit.policy_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        return f

    def broken(self) -> dict:
        # each way a teammate's commit can leave the policy's patterns unloaded -> the body of policy/audit.json
        return {"cut off": '{"redact": {"extra": ["' + self.PATTERN.replace("\\", "\\\\") + '"]',
                "extra is a string": json.dumps({"redact": {"extra": self.PATTERN}}),
                "redact is a list": json.dumps({"redact": [self.PATTERN]}),
                "not an object": json.dumps([self.PATTERN]),
                "a bad pattern beside it": json.dumps({"redact": {"extra": ["(", self.PATTERN]}}),
                "a misspelled key": json.dumps({"redact": {"extras": [self.PATTERN]}}),
                "empty file": ""}

    def test_a_broken_policy_withholds_the_whole_text_and_names_the_file(self):
        where = core.tilde(audit.policy_file())
        for name, body in self.broken().items():
            with self.subTest(policy=name):
                self.write_policy(body)
                self.assertEqual(audit.redact(self.TEXT), f"[text not recorded: {len(self.TEXT)} characters]")
                cfg = audit.settings()
                self.assertTrue(any(e.startswith(where) for e in cfg["errors"]), cfg["errors"])
                self.assertIn(where, audit.redaction_unavailable())

    def test_an_unreadable_policy_withholds_the_whole_text(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a file of mode 000")
        f = self.write_policy(json.dumps({"redact": {"extra": [self.PATTERN]}}))
        os.chmod(f, 0)  # the temporary folder's cleanup removes it all the same
        self.assertNotIn(self.TOKEN, audit.redact(self.TEXT))
        self.assertTrue(any("cannot be read" in e for e in audit.settings()["errors"]))
        self.assertIn(core.tilde(f), audit.redaction_unavailable())

    def test_callers_that_read_the_patterns_themselves_fail_closed(self):
        # messaging.secret_shaped asks the compiled patterns whether a text holds a secret; cli asks whether any
        # extra pattern exists before it trusts the built-in rules alone
        self.write_policy(json.dumps({"redact": {"extra": self.PATTERN}}))
        extra = audit.settings()["redact"]["extra"]
        self.assertTrue(extra)
        self.assertTrue(any(rx.search("plain words") for rx in audit._extra_rules(extra)))
        self.assertEqual(audit.redact("plain words", audit._extra_rules(extra)), "[text not recorded: 11 characters]")

    def test_withholding_twice_keeps_the_first_length(self):
        self.write_policy("{")
        once = audit.redact(self.TEXT)
        self.assertEqual(audit.redact(once), once)
        self.assertEqual(audit.redact(""), "")
        # each length marker the trail writes, cut (N+) or not, stays as it is
        for marker in ("[free text: 29 characters]", "[free text: 290+ characters]",
                       "[message text not recorded: 186+ characters]", "[question not recorded: 300+ characters]",
                       "[approval line not recorded: 12 characters]", "[text not recorded: 40+ characters]"):
            with self.subTest(marker=marker):
                self.assertEqual(audit.redact(marker), marker)

    def test_a_valid_policy_still_redacts_only_the_token(self):
        self.write_policy(json.dumps({"redact": {"extra": [self.PATTERN]}}))
        self.assertEqual(audit.redaction_unavailable(), "")
        self.assertEqual(audit.settings()["errors"], [])
        self.assertEqual(audit.redact(self.TEXT), "the deploy key " + audit.MARK + " was refused")

    def test_no_policy_file_is_no_failure(self):
        self.assertFalse(audit.policy_file().exists())
        self.assertEqual(audit.redaction_unavailable(), "")
        self.assertEqual(audit.redact("plain words stay"), "plain words stay")

    def test_a_record_written_meanwhile_keeps_lengths_and_says_why(self):
        self.write_policy('{"redact": ')
        rec = audit.record("guard", "deploy " + self.TOKEN, self.TEXT, "blocked", why="push " + self.TOKEN)
        raw = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        self.assertNotIn(self.TOKEN, raw)
        stored = [json.loads(ln) for ln in raw.splitlines() if '"guard"' in ln][-1]
        self.assertEqual(stored["id"], rec["id"])
        self.assertEqual((stored["action"], stored["outcome"]), ("guard", "blocked"))
        self.assertEqual(stored["target"], f"[text not recorded: {len('deploy ' + self.TOKEN)} characters]")
        self.assertEqual(stored["detail"], f"[text not recorded: {len(self.TEXT)} characters]")
        self.assertIn(core.tilde(audit.policy_file()), stored["ref"]["withheld"])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_reading_older_records_meanwhile_shows_no_free_text(self):
        # written before the team knew the token's shape: stored as it is, cleaned again whenever it is read
        audit.record("guard", "deploy", self.TEXT, "blocked")
        self.write_policy(json.dumps({"redact": {"extra": self.PATTERN}}))
        shown = [r for r in audit.query() if r["action"] == "guard"]
        self.assertEqual(len(shown), 1)
        self.assertNotIn(self.TOKEN, json.dumps(shown))
        self.assertEqual(shown[0]["detail"], f"[text not recorded: {len(self.TEXT)} characters]")

    def test_dev_audit_config_names_the_broken_file(self):
        import argparse
        self.write_policy("{")
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(["config"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(audit.cmd_audit(a), 0)
        lines = [ln for ln in out.getvalue().splitlines() if core.tilde(audit.policy_file()) in ln]
        self.assertTrue(any("not valid JSON" in ln for ln in lines), out.getvalue())
        self.assertTrue(any("withheld" in ln for ln in lines), out.getvalue())

    def test_a_message_is_refused_with_the_policy_to_repair_not_as_a_secret(self):
        # every text looks secret while the patterns cannot load, so the refusal names the file instead of the body
        from devkit import messaging
        self.write_policy(self.broken()["cut off"])
        with self.assertRaises(messaging.MessageError) as cm:
            messaging.validate_body("the build is green")
        self.assertEqual(cm.exception.code, "secret")
        self.assertIn(core.tilde(audit.policy_file()), str(cm.exception))
        self.assertNotIn("appears to contain a secret", str(cm.exception))
        # a session message's short fields (a question's title, a claim's reason) say the same
        from devkit import peers
        with self.assertRaises(peers.PeerError) as cm:
            peers._text("the build is green", 200, "the title")
        self.assertIn(core.tilde(audit.policy_file()), str(cm.exception))
        self.assertNotIn("looks like a secret", str(cm.exception))

    def test_a_withheld_answer_keeps_its_own_length(self):
        # the answer becomes its length before it is checked against the withheld option labels; that check keeps the
        # marker instead of measuring the marker itself
        self.write_policy('{"redact": ')
        rec = audit.record("crew.decided", "D3", "B", "ok", options=["A: csv", "B: xlsx"], answer="B")
        self.assertEqual(rec["detail"], "[text not recorded: 1 characters]")
        self.assertEqual(rec["ref"]["answer"], "[text not recorded: 1 characters]")
        audit.policy_file().unlink()  # repaired: read again, the record shows the same length
        shown = [r for r in audit.query() if r["action"] == "crew.decided"]
        self.assertEqual((shown[0]["detail"], shown[0]["ref"]["answer"]),
                         ("[text not recorded: 1 characters]", "[text not recorded: 1 characters]"))

    def test_an_answer_shaped_like_a_length_marker_keeps_only_its_length(self):
        # only the two markers an answer becomes stay; any other bracketed "<words>: N characters" is the person's text
        typed = "[correct horse battery staple: 4 characters]"
        hashed = "[approval line not recorded: 1 characters, sha256 " + "9f" * 32 + "]"
        options = ["A: csv", "B: xlsx"]
        for answer, kept in ((typed, f"[free text: {len(typed)} characters]"),
                             (hashed, f"[free text: {len(hashed)} characters]"),
                             ("[free text: 10 characters]", "[free text: 10 characters]"),
                             ("[text not recorded: 1 characters]", "[text not recorded: 1 characters]")):
            with self.subTest(answer=answer):
                rec = audit.record("crew.decided", "D3", answer, "ok", options=options, answer=answer)
                self.assertEqual((rec["detail"], rec["ref"]["answer"]), (kept, kept))
        # while the patterns cannot load, such a text is withheld like any other, not kept as a marker
        self.write_policy('{"redact": ')
        rec = audit.record("guard", "deploy", typed, "blocked")
        self.assertEqual(rec["detail"], f"[text not recorded: {len(typed)} characters]")

    def test_a_record_keeps_devkits_own_ids_and_words_while_text_is_withheld(self):
        session = "claude:0f8e2c1a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
        registry = {"projects": [{"id": "acme/api"}]}
        self.write_policy('{"redact": ')
        with mock.patch.object(core, "read_registry", return_value=registry):
            rec = audit.record("crew.withdrawn", "D3", "no longer applies", "withdrawn", project="acme/api",
                               by="morpheus", why="no longer applies", was="pending", session=session)
            other = audit.record("crew.withdrawn", "D3 " + self.TOKEN, "x", "withdrawn",
                                 project="acme/" + self.TOKEN, by="morpheus-" + self.TOKEN, was="pending " + self.TOKEN,
                                 session="claude:" + self.TOKEN)
            # a member's id after a prefix ("devkit:neo" names Neo) is no member when the prefix is a token
            prefixed = audit.record("crew.withdrawn", self.GITHUB + ":neo", "x", "withdrawn",
                                    by=self.TOKEN + ":tank", was="pending")
            members = [audit.record("crew.withdrawn", "D3", "x", "withdrawn", by=name, was="pending")["ref"]["by"]
                       for name in ("neo", "Neo", "The Keymaker", "devkit:neo")]
            found = [r["id"] for r in audit.query(project="acme/api")]  # what dev audit show --project finds
        self.assertEqual((rec["target"], rec["project"]), ("D3", "acme/api"))
        self.assertEqual(rec["ref"]["by"], "morpheus")
        self.assertEqual((rec["ref"]["was"], rec["ref"]["session"], rec["ref"]["result"]),
                         ("pending", session, "withdrawn"))
        self.assertEqual(rec["detail"], "[text not recorded: 17 characters]")  # free text stays withheld
        self.assertEqual(rec["why"], "[text not recorded: 17 characters]")
        # the same fields with anything around the id or word: the whole value is withheld
        self.assertNotIn(self.TOKEN, json.dumps(other))
        for value in (other["target"], other["project"], other["ref"]["by"], other["ref"]["was"],
                      other["ref"]["session"], prefixed["target"], prefixed["ref"]["by"]):
            self.assertRegex(value, r"^\[text not recorded: \d+ characters\]$")
        self.assertNotIn(self.GITHUB, json.dumps(prefixed))
        self.assertEqual(members, ["neo", "Neo", "The Keymaker", "devkit:neo"])
        self.assertEqual(found, [rec["id"]])

    def test_a_policy_file_is_publishable_only_when_every_mac_could_load_it(self):
        # what dev sync asks before it commits a team policy file: the reason settings() would give, or ""
        folder = self.root / "handoff-copy" / "policy"
        folder.mkdir(parents=True)
        cases = {("audit.json", '{"redact": '): "is not valid JSON", ("audit.json", ""): "is not valid JSON",
                 ("audit.json", json.dumps([self.PATTERN])): "is not a JSON object",
                 ("audit.json", json.dumps({"audit.redact.extra": [self.PATTERN]})): "has an unknown key",
                 ("audit.json", json.dumps({"redact": {"extra": ["("]}})): "rejected",
                 ("audit.json", json.dumps({"redact": {"extra": [self.PATTERN]}, "level": "all"})): "",
                 ("progress.json", "{"): "is not valid JSON", ("progress.json", json.dumps({"max_parallel": 4})): ""}
        for (name, body), why in cases.items():
            with self.subTest(file=name, body=body):
                (folder / name).write_text(body)
                got = audit.publishable_policy(folder / name)
                if why:
                    self.assertIn(why, got)
                else:
                    self.assertEqual(got, "")
        self.assertEqual(audit.publishable_policy(folder / "gone.json"), "")  # a deletion publishes nothing broken
        self.assertFalse(audit.policy_file().exists())  # it reads the file it is given, not the team's

    def test_a_kept_value_has_an_id_length_devkit_writes_and_no_pattern_that_loaded_names_it(self):
        # the team's policy has a key this Mac does not know, so its patterns load but text is withheld; this Mac's
        # pattern names one project, the team's another
        registry = {"projects": [{"id": "acme/api"}, {"id": "acme/zeus-client"}, {"id": "acme/orion-bid"}]}
        core.CFG["audit"]["redact"] = {"extra": [r"zeus-client"]}
        self.write_policy(json.dumps({"redact": {"extra": [r"orion-bid"]}, "archive": True}))
        values = {"T-ip4k2a7c9x1z": True, "L-fy15osnf0z3": True, "S-ab12cd34": True, "E-ab12cd34": True,
                  "D12": True, "acme/api": True,
                  "P-x7k2m9q4w8z1n5v3": False, "T-abcd": False, "E-ab12cd34ef56": False,
                  "acme/zeus-client": False, "acme/orion-bid": False}
        with mock.patch.object(core, "read_registry", return_value=registry):
            self.assertIn("unknown key", audit.redaction_unavailable())
            got = {v: audit.record("crew.withdrawn", "D3", "x", "withdrawn", project=v if "/" in v else "",
                                   by=v, was="pending") for v in values}
        for value, kept in values.items():
            with self.subTest(value=value):
                shown = got[value]["ref"]["by"]
                self.assertEqual(shown, value if kept else f"[text not recorded: {len(value)} characters]")
                if "/" in value:
                    self.assertEqual(got[value]["project"], shown)

    def test_the_reason_names_an_unknown_key_only_when_it_is_a_short_word_no_pattern_names(self):
        # a token pasted as a key: the reason goes into every record (ref.withheld), every refusal and dev sync's
        # handoff.push record, so a key is named only when it is a short word that neither a built-in rule nor a
        # pattern that loaded names; any other key is its length
        team_word = "zuluquebec"
        cases = {json.dumps({self.GITHUB: True}):
                     (self.GITHUB, f"an unknown key of {len(self.GITHUB)} characters in audit"),
                 json.dumps({"retention": {self.GITHUB: 3}}):
                     (self.GITHUB, f"an unknown key of {len(self.GITHUB)} characters in audit.retention"),
                 json.dumps({"acmetok7f3k9x2m4q8zr1": True}):
                     ("acmetok7f3k9x2m4q8zr1", "an unknown key of 21 characters in audit"),
                 json.dumps({"redact": {"extra": [team_word]}, team_word: True}):
                     (team_word, f"an unknown key of {len(team_word)} characters in audit"),
                 json.dumps({"redcat": {"extra": [self.PATTERN]}}): ("", "has an unknown key audit.redcat"),
                 json.dumps({"retention": {"extra": [self.PATTERN]}}): ("", "has an unknown key audit.retention.extra"),
                 json.dumps({"audit.redact.extra": [self.PATTERN]}):
                     ("", 'has an unknown key audit["audit.redact.extra"]')}
        folder = self.root / "handoff-copy" / "policy"
        folder.mkdir(parents=True)
        for body, (secret, said) in cases.items():
            with self.subTest(policy=body[:30]):
                self.write_policy(body)
                (folder / "audit.json").write_text(body)
                rec = audit.record("guard", "deploy", "x", "blocked")
                texts = {"withheld": rec["ref"]["withheld"], "refusal": audit.redaction_unavailable(),
                         "sync": audit.publishable_policy(folder / "audit.json"),
                         "dev audit config": " ".join(audit.settings()["errors"])}
                for where, text in texts.items():
                    if secret:
                        self.assertNotIn(secret, text, where)
                    if where != "dev audit config":
                        self.assertIn(said, text, where)
                if secret:
                    self.assertNotIn(secret, json.dumps(rec))
        # dev audit config quotes a wrong value only when it is a number, true, false or a short word
        self.write_policy(json.dumps({"publish": self.GITHUB, "level": "everything", "hold": 3}))
        errors = " ".join(audit.settings()["errors"])
        self.assertNotIn(self.GITHUB[:12], errors)
        self.assertIn("audit.publish must be true or false (got a value it does not quote)", errors)
        self.assertIn('(got "everything")', errors)
        self.assertIn("(got 3)", errors)

    def test_dev_audit_config_says_a_nested_unknown_key_by_its_own_length_and_group(self):
        # a token pasted as a key inside retention was said as "of 62 characters in audit": the group's name counted
        # in its length, and the place one level too high. A top-level key that has a dot is not in a group.
        dotted = "retention." + self.GITHUB
        for where, body, said in (("team", {"retention": {self.GITHUB: 3}},
                                   f"unknown key of {len(self.GITHUB)} characters in audit.retention"),
                                  ("team", {dotted: 3}, f"unknown key of {len(dotted)} characters in audit"),
                                  ("machine", {"retention": {self.GITHUB: 3}},
                                   f"unknown key of {len(self.GITHUB)} characters in audit.retention")):
            with self.subTest(where=where, body=list(body)[0][:12]):
                if where == "team":
                    self.write_policy(json.dumps(body))
                else:
                    audit.policy_file().unlink()
                    core.CFG["audit"] = body
                errors = audit.settings()["errors"]
                self.assertTrue(any(e.endswith(said) for e in errors), errors)
                self.assertNotIn(self.GITHUB, " ".join(errors))

    def test_an_unknown_key_in_the_team_policy_withholds_text(self):
        # a misspelled or misplaced section may hold the team's patterns: the docs call them audit.redact.extra
        for name, body in {"misspelled": {"redcat": {"extra": [self.PATTERN]}},
                           "capitalized": {"Redact": {"extra": [self.PATTERN]}},
                           "wrapped in audit": {"audit": {"redact": {"extra": [self.PATTERN]}}},
                           # the docs' own dotted notation as a key, and a dotted or nested key in any case or group
                           "dotted in full": {"audit.redact.extra": [self.PATTERN]},
                           "dotted section": {"audit.redact": {"extra": [self.PATTERN]}},
                           "dotted and capitalized": {"Redact.extra": [self.PATTERN]},
                           "extra under retention": {"retention": {"extra": [self.PATTERN]}}}.items():
            with self.subTest(policy=name):
                self.write_policy(json.dumps(body))
                self.assertEqual(audit.redact(self.TEXT), f"[text not recorded: {len(self.TEXT)} characters]")
                self.assertIn("unknown key", audit.redaction_unavailable())
        # this Mac's own audit section fails the same way, and when it is not an object at all
        audit.policy_file().unlink()
        for name, own in {"dotted key": {"redact.extra": [self.PATTERN]}, "a list": [self.PATTERN],
                          "extra under retention": {"retention": {"extra": [self.PATTERN]}}}.items():
            with self.subTest(machine=name):
                core.CFG["audit"] = own
                self.assertEqual(audit.redact(self.TEXT), f"[text not recorded: {len(self.TEXT)} characters]")
                self.assertIn(core.tilde(core.CONFIG_FILE), audit.redaction_unavailable())
        core.CFG["audit"] = json.loads(json.dumps(core.DEFAULTS["audit"]))
        # a known key with a wrong value outside redact still lets the team's patterns load
        self.write_policy(json.dumps({"redact": {"extra": [self.PATTERN]}, "retention": "x"}))
        self.assertEqual(audit.redaction_unavailable(), "")
        self.assertEqual(audit.redact(self.TEXT), "the deploy key " + audit.MARK + " was refused")

    def screens(self, text: str) -> dict:
        # every place devkit screens text for secrets with a verdict of its own -> what it said about `text`
        from devkit import bugs, hooks, lessonlint
        return {"bug report": bugs._secret_like(text), "lesson": lessonlint.screen({"Prevention": text}),
                "payload capture": hooks._scrub({"prompt": text}, False)["prompt"]}

    def test_every_secret_screen_uses_the_team_and_machine_patterns(self):
        # letters only, so no built-in rule and no heuristic of the bug screen (hex, long mixed runs) knows the shape
        token, pattern = "acme-zulu-quebec-tango", r"acme-zulu-[a-z]+-[a-z]+"
        text = "deploy with " + token
        self.assertEqual(self.screens(text), {"bug report": False, "lesson": "", "payload capture": text})
        for where in ("team", "machine"):
            with self.subTest(patterns=where):
                if where == "team":
                    self.write_policy(json.dumps({"redact": {"extra": [pattern]}}))
                else:
                    audit.policy_file().unlink()
                    core.CFG["audit"]["redact"] = {"extra": [pattern]}
                got = self.screens(text)
                self.assertIs(got["bug report"], True)
                self.assertIn("secret", got["lesson"])
                self.assertNotIn(token, got["payload capture"])
                self.assertEqual(self.screens("plain words"),
                                 {"bug report": False, "lesson": "", "payload capture": "plain words"})

    def test_every_secret_screen_fails_closed_while_patterns_cannot_load(self):
        cases = {"team policy cut off": ('{"redact": ', None),
                 "machine extra written as a string": (None, {"extra": r"acme-zulu-[a-z]+-[a-z]+"}),
                 "machine pattern that does not compile": (None, {"extra": ["(", r"acme-zulu-[a-z]+-[a-z]+"]})}
        for name, (policy, own) in cases.items():
            with self.subTest(case=name):
                if policy is not None:
                    self.write_policy(policy)
                elif audit.policy_file().exists():
                    audit.policy_file().unlink()
                core.CFG["audit"]["redact"] = own or {"extra": []}
                reason = audit.redaction_unavailable()
                self.assertIn(core.tilde(audit.policy_file() if policy else core.CONFIG_FILE), reason)
                got = self.screens("plain words")
                self.assertIs(got["bug report"], True)
                self.assertIn("cannot be checked for secrets", got["lesson"])
                self.assertEqual(got["payload capture"], "[text not recorded: 11 characters]")
                self.assertEqual(audit.redact("plain words"), "[text not recorded: 11 characters]")


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

    def test_a_record_takes_a_reserved_id_once_and_no_id_of_the_callers_own(self):
        # the acceptance list names its ext.accept record before the record is written: the id is reserved first,
        # and a caller can neither give a record an id of its own nor reuse one
        rid = audit.reserve_id()
        a = audit.record("ext.accept", "plugin:claude:x@y", "accepted", "accepted", reserved_id=rid)
        b = audit.record("ext.accept", "plugin:claude:x@y", "accepted", "accepted", reserved_id=rid)
        c = audit.record("ext.accept", "plugin:claude:x@y", "accepted", "accepted", reserved_id="0" * 26)
        self.assertEqual(a["id"], rid)
        self.assertNotIn(b["id"], (rid, "0" * 26))
        self.assertNotEqual(c["id"], "0" * 26)
        self.assertEqual([r["id"] for r in self.lines() if r["action"] == "ext.accept"], [a["id"], b["id"], c["id"]])
        self.assertEqual(audit.check_record(a), [])

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
        self.assertEqual((r["project"], r["branch"], r["run"], r["step"]),
                         ("acme/app", "feat/x", self.run_ref("claude", "sess-1"), "2"))  # the plan's second step
        main = audit.query(action="dev.task")[0]
        self.assertNotIn("crew", main)
        self.assertEqual((main["project"], main["run"]), ("acme/app", self.run_ref("claude", "sess-1")))

    def test_a_hooks_agent_id_prefix_finds_the_lane(self):
        self.crew_run()
        audit.record("crew.bind", "Tank", "Tank: the API", agent_id="a1b2c3d4e5f6a7b8", launch="toolu_01ABC")
        crew = audit.query(action="crew.bind")[-1]["crew"]
        self.assertEqual((crew["member"], crew["lane"]), ("tank", "a1b2c3d4e5f6a7b8c9"))

    def test_a_launch_label_is_recorded_as_its_length(self):
        # a launch's description is free text (`Tank: the Acme payroll API`): crew.bind, crew.start and crew.resume
        # kept it in clear; the member's roster name and devkit's own note stay
        t = audit.TEXT_MARK.format
        audit.record("crew.bind", "Tank", "Tank: the Acme payroll API", agent_id="a1b2c3d4e5f6a7b8")
        audit.record("crew.start", "Tank", "Tank: the Acme payroll API", agent_id="a1b2c3d4e5f6a7b8")
        audit.record("crew.start", "general-purpose", "waits for its launch", agent_id="a1b2c3d4e5f6a7b8")
        audit.record("crew.resume", "Tank", "Tank: the Acme payroll API", agent_id="a1b2c3d4e5f6a7b8")
        audit.record("crew.bind", "globex payroll check", "globex payroll check", agent_id="a1b2c3d4e5f6a7b8")
        audit.record("crew.queued", "The Keymaker", "no crew slot is free", "queued", agent_id="a1b2c3d4e5f6a7b8")
        got = [(r["action"], r["target"], r["detail"]) for r in self.lines() if r["action"].startswith("crew.")]
        self.assertEqual(got, [("crew.bind", "Tank", t(n=26)), ("crew.start", "Tank", t(n=26)),
                               ("crew.start", "general-purpose", "waits for its launch"),
                               ("crew.resume", "Tank", t(n=26)), ("crew.bind", t(n=20), t(n=20)),
                               ("crew.queued", "The Keymaker", "no crew slot is free")])
        stored = (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text()
        for words in ("Acme", "globex"):
            self.assertNotIn(words, stored)

    def test_extras_go_only_into_declared_fields(self):
        audit.record("plan.done", "api", "", "ok", project="acme/app", run="Invoice export", by="Morpheus",
                     was="running", id="M3", sha="abc1234", member="Neo", rules=["scope", "lane"], bogus="x" * 9)
        r = audit.query(action="plan.done")[-1]
        self.assertTrue(set(r) <= set(audit.SCHEMA), set(r) - set(audit.SCHEMA))
        self.assertEqual(r["ref"], {"by": "Morpheus", "was": "running", "id": "M3"})
        self.assertEqual(r["evidence"], {"commit": "abc1234", "rule": "scope, lane"})
        self.assertEqual(r["crew"]["member"], "neo")
        self.assertNotIn("xxxxxxxxx", json.dumps(r))

    def test_automatic_authorization_preserves_its_policy_and_principal_evidence(self):
        fields = {"operation": "release", "mode_source": "session", "policy_digest": "ab" * 32,
                  "origin_repo": "acme/tool", "account": "github:12345", "principal_kind": "main"}
        audit.record("crew.authorization", "v26.11.19", "automatic operation policy", "allowed",
                     source="policy-auto", **fields)
        recorded = audit.query(action="crew.authorization")[-1]["ref"]
        self.assertEqual({key: recorded.get(key) for key in fields}, fields)
        self.assertEqual(recorded["source"], "policy-auto")

    def test_a_codex_thread_shared_by_a_member_and_its_caller_is_a_security_notice(self):
        # identity records it when a member's command carries another caller's Codex thread
        self.assertEqual(audit.kind("codex.thread-shared"), ("security", "notice"))

    def test_every_action_the_code_records_is_registered(self):
        found = set()  # the identity records too (identity._audit_identity), which once went unregistered
        call = re.compile(r"""\b(?:audit\.record|_audit|_audit_identity)\(\s*f?["']([a-z][a-z0-9_.-]*)""")
        for f in sorted((LIB / "devkit").glob("*.py")):
            found |= {m.group(1).rstrip(".") for m in call.finditer(f.read_text())}
        found.discard("action")
        self.assertGreater(len(found), 25)
        for action in sorted(found):
            with self.subTest(action=action):
                self.assertTrue(audit.registered(action), f"{action} is not in audit.ACTIONS")

    def test_the_gate_and_scope_records_keep_their_fields(self):
        # the records earlier versions wrote at the irreversible-action gate and a bypass, and scope.write detection,
        # pass these; EXTRAS dropped them
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
            for m in re.finditer(r"(?:audit\.record|\b_audit)\(", s):
                i, depth = m.end(), 1
                while depth and i < len(s):
                    depth += {"(": 1, ")": -1}.get(s[i], 0)
                    i += 1
                call = re.sub(r"\([^()]*\)", "()", re.sub(r"\([^()]*\)", "()", s[m.end():i - 1]))
                for k in re.findall(r"(?:^|,)\s*([a-z_]+)=", call):
                    found.setdefault(k, f.name)
        for name, param in inspect.signature(audit.record).parameters.items():  # record's own: outcome, reserved_id
            if param.kind is not inspect.Parameter.VAR_KEYWORD:
                found.pop(name, None)
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
            (["ask", "--decision", "which roadmap for Acme?", "--options", "A: ship | B: wait", "--recommend", "A"],
             "ask --decision [message text not recorded: 23 characters] --options"),
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
        self.assertIn("[redacted]", r["branch"])
        self.assertEqual((r["run"], r["step"]), (self.run_ref("claude", "sess-2"), "1"))
        self.assertNotIn(A36[4:], json.dumps(r))
        self.assertNotIn(VALUE, json.dumps(r))

    def test_a_run_goal_and_a_step_name_never_reach_a_record(self):
        # under D6 = A the trail goes to the handoff repository for 13 months: a run's goal and a step's name are
        # free text that can name a client or a person. A record names the run by its id and the step by its number
        # in the plan, which name nothing and still tie it to them; where an action names a goal or a step (a plan
        # step's change, a run replaced or orphaned, a run a session adopts, a step handed to a member), and where a
        # caller passes one, only its length stays
        os.environ["DEVKIT_SESSION"] = "claude:sess-3"
        progress.SESSIONS.mkdir(parents=True)
        (progress.SESSIONS / "claude-sess-3.json").write_text(json.dumps(
            {"agent": "claude", "session": "sess-3", "project": "acme/app", "branch": "main"}))
        progress.RUNS.mkdir(parents=True)
        (progress.RUNS / "claude-sess-3.json").write_text(json.dumps({
            "v": 2, "agent": "claude", "session": "sess-3", "project": "acme/app", "goal": "Globex payroll migration",
            "agents": {}, "plan": [{"name": "Initech export", "state": "running", "owner": "main"}]}))
        goal, step = "[text not recorded: 24 characters]", "[text not recorded: 14 characters]"
        audit.record("dev.task", "add x", "", "ok")
        audit.record("crew.decline", "Neo", "not mine", step="Initech export")  # dispatch names the step
        audit.record("plan.done", "Initech export", "12 tests pass", "ok", run="Globex payroll migration")
        audit.record("progress.replace", "Globex payroll migration", "a new goal", "ok")
        audit.record("run.orphaned", "Globex payroll migration", "its session ended", "orphaned")
        audit.record("session.adopt", "claude:abc", "run «Globex payroll migration» from claude:def (process 4)")
        audit.record("crew.scope-handed", "Tank", "“Initech export” went to Neo: this lane may read", "restricted")
        task, done, replace, orphaned, adopt, handed = (audit.query(action=a)[-1] for a in (
            "dev.task", "plan.done", "progress.replace", "run.orphaned", "session.adopt", "crew.scope-handed"))
        self.assertEqual((task["run"], task["step"]), (self.run_ref("claude", "sess-3"), "1"))
        self.assertEqual(audit.query(action="crew.decline")[-1]["step"], step)
        self.assertEqual((done["target"], done["run"], done["detail"]), (step, goal, "12 tests pass"))
        self.assertEqual((replace["target"], replace["detail"]), (goal, "a new goal"))
        self.assertEqual(orphaned["target"], goal)
        self.assertEqual(adopt["detail"], f"run «{goal}» from claude:def (process 4)")
        self.assertEqual(handed["detail"], f"“{step}” went to Neo: this lane may read")
        audit.record("guard.deny", 'dev codex "Globex secret prompt"', "refused for the test", "deny", tool="Bash")
        self.assertEqual(audit.query(action="guard.deny")[-1]["target"],
                         "dev codex [text not recorded: 20 characters]")  # a prompt where the project query goes
        for words in ("Globex", "Initech", "payroll"):
            self.assertNotIn(words, (audit.AUDIT / time.strftime("%Y-%m.jsonl")).read_text())
        self.assertEqual([r["action"] for r in audit.query(days=1) if r.get("cleaned")], [])

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

    def test_message_retry_and_cursor_values_do_not_leak_free_text_into_audit(self):
        audit.record("dev.crew", ["ask", "--request-id", "private-project", "private-body"], "", "ok")
        audit.record("dev.crew", ["read", "M12", "--cursor", "private-cursor"], "", "ok")
        targets = [r["target"] for r in audit.query(action="dev.crew")]
        self.assertEqual(targets, [f"ask --request-id {self.m(15)} {self.m(12)}",
                                   f"read M12 --cursor {self.t(14)}"])
        for value in ("private-project", "private-body", "private-cursor"):
            self.assertNotIn(value, self.stored())

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
            ("note", ["set", "milestone=SECRET launch", "status=blocked"], f"set milestone={self.t(13)} status=blocked"),
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
                     "ask --to Persephone [message text not recorded: 44 characters]",  # a marker: read as stored
                 # there only the option names dev's parser knows stay, and numbers of up to
                 # six digits (a phone number is no task number)
                 ("task", "done 3 --why called Ana at 6505551234 about --acme-merger --json"):
                     f"done 3 --why {self.t(44)} --json",
                 ("task", "done 1234567"): f"done {self.t(7)}"}
        for (cmd, joined), kept in cases.items():
            with self.subTest(joined=joined[:16]):
                self.assertEqual(audit.mask_command(joined, "argv", ("dev", cmd), 300), kept)
        # free text starts after the last subcommand: an option before it keeps its name, so the subcommand reads the
        # same when the record is read again
        once = audit.mask_command("-u note set milestone=x --why a --acme-merger", "argv", ("dev",), 300)
        self.assertEqual(once, f"-u note {self.t(15)} --why {self.t(15)}")
        self.assertEqual(audit.mask_command(once, "argv", ("dev",), 300), once)

    def test_a_selector_stays_only_when_it_is_an_id_or_a_number(self):
        # M2: dev task done "Acme export" selects a task by a part of its title
        cases = [("task", ["done", "3"], "done 3"), ("task", ["done", "T-fwk7a2abc"], "done T-fwk7a2abc"),
                 ("task", ["done", "SECRET", "Acme", "export"], f"done {self.t(18)}"),
                 ("lesson", ["recur", "L-fz3qnrebvm5", "--evidence", "SECRET"], f"recur L-fz3qnrebvm5 --evidence {self.t(6)}"),
                 ("crew", ["answer", "M12", "yes"], f"answer M12 {self.m(3)}"),
                 ("crew", ["decline", "SECRET step", "--why", "x"], f"decline {self.t(11)} --why {self.t(1)}"),
                 ("progress", ["assign", "2", "--to", "Tank", "--scope", "lib/**"], "assign 2 --to Tank --scope lib/**"),
                 ("task", ["done", "6505551234"], f"done {self.t(10)}")]  # L2: a number of more than six digits
        for cmd, argv, kept in cases:
            with self.subTest(argv=argv[:2]):
                audit.record("dev." + cmd, argv, "", "ok")
                self.assertEqual(audit.query(action="dev." + cmd)[-1]["target"], kept)
        self.assertEqual(self.denied("dev task done 3 --why called 6505551234", tool="Bash"),
                         f"dev task done 3 --why {self.t(6)} {self.t(10)}")
        self.assertNotIn("SECRET", self.stored())
        self.assertNotIn("6505551234", self.stored())

    def test_crew_raci_keeps_a_kind_of_work_or_a_crew_id_and_masks_anything_else(self):
        # dev crew raci also classifies a task given in words. Its argument stays only as a
        # kind of work or a crew id of devkit's roster, and its --set value only as R/A/C/I pairs of crew ids
        t = self.t
        cases = [(["raci", "backend"], "raci backend"), (["raci", "tank"], "raci tank"), (["raci", "Scout"], "raci Scout"),
                 (["raci", "SECRET-merger"], f"raci {t(13)}"),
                 (["raci", "classify the Acme SECRET merger work"], f"raci {t(36)}"),
                 (["raci", "review", "--set", "R=seraph,neo A=morpheus", "--why", "x"],
                  f"raci review --set R=seraph,neo A=morpheus --why {t(1)}"),
                 (["raci", "review", "--set", "R=seraph A=SECRET", "--why", "x"], f"raci review --set {t(17)} --why {t(1)}")]
        for argv, kept in cases:
            with self.subTest(argv=argv[1]):
                audit.record("dev.crew", argv, "", "ok")
                self.assertEqual(audit.query(action="dev.crew")[-1]["target"], kept)
        self.assertEqual(self.denied('dev crew raci "classify the Acme SECRET merger work"', tool="Bash"),
                         f"dev crew raci {t(36)}")
        self.assertEqual(self.denied("dev crew raci review --set 'R=seraph A=SECRET'", tool="Bash"),
                         f"dev crew raci review --set {t(17)}")
        self.assertNotIn("SECRET", self.stored())
        self.unmarked()

    def test_dev_note_set_keeps_a_value_only_from_a_keys_fixed_set(self):
        # status and priority take a value from a fixed set; a milestone, a due date or tags
        # are the developer's words
        t = self.t
        audit.record("dev.note", ["set", "status=blocked", "priority=high", "milestone=3", "due=2026-12-01",
                                  "status=SECRET", "tags=acme"], "", "ok")
        self.assertEqual(audit.query(action="dev.note")[-1]["target"],
                         f"set status=blocked priority=high milestone={t(1)} due={t(10)} status={t(6)} tags={t(4)}")
        self.assertNotIn("SECRET", self.stored())

    def test_a_shell_command_keeps_its_options_and_ids_but_not_its_free_text(self):
        # what the hooks keep of a refused call: the shell text, quotes, operators and redirections included
        cases = {
            'dev crew answer M12 "yes, ship the Acme roadmap"': f"dev crew answer M12 {self.m(26)}",
            'dev crew decide D3 "after the Acme call"': "dev crew decide D3 [free text: 19 characters]",
            'dev progress step "Acme parser done" --check="12 tests pass" --next "review"':
                f"dev progress step {self.t(16)} --check={self.t(13)} --next {self.t(6)}",
            'cd ~/x && dev handoff --log "shipped; next: Acme rooms" > /dev/null 2>&1':
                f"cd {self.t(3)} && dev handoff --log {self.t(25)} > /dev/null 2>&1",
            "git commit -am \"fix: keep Acme's totals\" && git push": f"git commit -am {self.t(23)} && git push",
            "gh pr create --title \"Acme export\" --body \"Ana's notes\" --base main":
                f"gh pr create --title {self.t(11)} --body {self.t(11)} --base main",
            'git commit -m "-WIP SECRET"': f"git commit -m {self.t(11)}",  # L2: a value that starts with -
            'dev task add "-e SECRET plan"': f"dev task add {self.t(14)}",
            'dev task add -- "-secret SECRET"': f"dev task add -- {self.t(14)}",
            "gh api repos/a/b/issues -f body='SECRET'": f"gh api {self.t(16)} -f {self.t(11)}",
            'dev crew set tank mission "SECRET mission" --why x': f"dev crew set tank mission {self.t(14)} --why {self.t(1)}",
            'dev note set milestone="SECRET launch"': f"dev note set milestone={self.t(13)}",
            'dev release 26.11.20 --why "SECRET launch"': f"dev release 26.11.20 --why {self.t(13)}",  # as --version
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("Acme", "Ana", "roadmap", "shipped", "SECRET"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_name_value_with_a_space_or_in_ansi_c_quotes_is_masked_in_a_shell_line_too(self):
        # a name option keeps one word that names something: a quoted value with a space, an ANSI-C $'...' word
        # whose escapes hold one (or a quote), or a marker glued to other text, is free text as it is in dev's
        # argument list, whatever the option and the form. --command and --section hold free text in every command
        cases = {
            'dev progress step "x" --step "Acme payroll migration"': f"dev progress step {self.t(1)} --step {self.t(22)}",
            'dev crew ask --step api --scope "secret client path/**" q':
                f"dev crew ask --step api --scope {self.m(21)} {self.m(1)}",
            "dev crew ask --to $'acme secret' q": f"dev crew ask --to {self.m(11)} {self.m(1)}",
            'dev crew ask --to="acme secret" q': f"dev crew ask --to={self.m(11)} {self.m(1)}",
            "dev crew ask --to $'acme\\x20secret' q": f"dev crew ask --to {self.m(11)} {self.m(1)}",
            "dev crew ask --to $'it\\'s acme' q": f"dev crew ask --to {self.m(9)} {self.m(1)}",
            "dev crew ask --to=$'acme\\nsecret' q": f"dev crew ask --to={self.m(11)} {self.m(1)}",
            "dev crew ask --to $'tank' q": f"dev crew ask --to $'tank' {self.m(1)}",  # one word, as --to tank
            'dev lesson add x --command curl': f"dev lesson add {self.t(1)} --command {self.t(4)}",
            'dev handoff --section "Acme private roadmap"': f"dev handoff --section {self.t(20)}",
            'dev crew ask --to tank --scope "services/**" q': f'dev crew ask --to tank --scope "services/**" {self.m(1)}',
            f'dev crew ask --to "{self.t(5)}acme" q': f"dev crew ask --to {self.m(37)} {self.m(1)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("Acme", "acme", "payroll", "secret client", "curl", "roadmap"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_shell_line_finds_its_program_behind_reserved_words_wrappers_and_nested_shells(self):
        # H2: the Code reviewer's forms; the program is found and its free text masked, and the words around it that
        # are not known to be safe become their length. The program is the word at command position: a
        # wrapper's options and their values come before it
        t, m = self.t, self.m
        cases = {
            '{ dev crew ask "SECRET plan"; }': f"{{ dev crew ask {m(11)}; }}",
            'if true; then dev crew ask "SECRET plan"; fi': f"if true; then dev crew ask {m(11)}; fi",
            'for x in a; do dev task add "SECRET plan"; done':
                f"for {t(1)} {t(2)} {t(1)}; do dev task add {t(11)}; done",
            '! dev crew ask "SECRET plan"': f"! dev crew ask {m(11)}",
            'timeout 60 dev crew ask "SECRET plan"': f"timeout 60 dev crew ask {m(11)}",
            'timeout -s KILL 5 dev crew ask "SECRET"': f"timeout -s {t(4)} 5 dev crew ask {m(6)}",
            'env -i -u HOME PATH=/x dev crew ask "SECRET"': f"env -i -u {t(4)} PATH={t(2)} dev crew ask {m(6)}",
            'uv run --with rich dev crew ask "SECRET"': f"uv run --with {t(4)} dev crew ask {m(6)}",
            'echo 1 | xargs -n 1 dev crew ask "SECRET plan"': f"echo {t(1)} | xargs -n 1 dev crew ask {m(11)}",
            'bash -c \'dev crew ask "SECRET plan"\'': f"bash -c 'dev crew ask {m(11)}'",
            "eval 'dev crew ask SECRET'": f"eval 'dev crew ask {m(6)}'",
            'zsh -lc "git commit -m \\"SECRET fix\\" && git push"': f'zsh -lc "git commit -m {t(10)} && git push"',
            'sudo -u root dev crew ask "SECRET"': f"sudo -u {t(4)} dev crew ask {m(6)}",
            'nice -n 5 dev crew ask "SECRET"': f"nice -n 5 dev crew ask {m(6)}",
            'uv run dev crew ask "SECRET"': f"uv run dev crew ask {m(6)}",
            'python3 -I -m devkit crew ask "SECRET"': f"python3 -I -m devkit crew ask {m(6)}",
            '~/Development/x/bin/dev crew ask "SECRET"': f"{t(19)}/dev crew ask {m(6)}",
            'dev crew ask <<< "SECRET"': f"dev crew ask <<< {t(6)}",
            'time -p dev --json crew ask "SECRET"': f"time -p dev --json crew ask {m(6)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        self.denied("dev task done 3 --why $'Ana\\'s SECRET review'")  # L1's form: an escaped quote in $'…'
        self.assertNotIn("SECRET", self.stored())
        self.unmarked()

    def test_a_line_without_a_known_program_keeps_its_operators_its_programs_name_and_each_words_length(self):
        # the program's name stays, its arguments become their length; a program run by its
        # path keeps only its base name, and a word that names no program (a variable, a quoted text) is masked
        t = self.t
        cases = {
            "ls -la /tmp # SECRET notes": f"ls {t(3)} {t(4)} # {t(12)}",
            "rm -rf src && python3 -m unittest tests.test_audit > out.log 2>&1":
                f"rm {t(3)} {t(3)} && python3 {t(2)} {t(8)} {t(16)} > {t(7)} 2>&1",
            "cat > notes.md <<'EOF'\nAna's salary review\nEOF": f"cat > {t(8)} <<'EOF'\n{t(19)}\nEOF",
            "cat <<\\EOF\nSECRET line\nEOF": f"cat <<\\EOF\n{t(11)}\nEOF",  # L3: a heredoc whose delimiter is escaped
            "echo SECRET | tee -a notes.md >/dev/null": f"echo {t(6)} | tee {t(2)} {t(8)} >/dev/null",
            "sleep 900": f"sleep {t(3)}",
            "/opt/homebrew/bin/python3 ~/acme/run.py": f"{t(17)}/python3 {t(13)}",
            "./gradlew build": f"{t(1)}/gradlew {t(5)}",
            '"$RUN" SECRET': f"{t(4)} {t(6)}",
            "'Ana notes' x": f"{t(9)} {t(1)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("SECRET", "Ana", "unittest", "notes.md", "homebrew", "acme"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_dev_is_a_program_only_at_command_position(self):
        # `dev` as another program's argument (a context, a container, a script, a host, an
        # echoed word, a file) is no program, so the words after it are masked like that program's other arguments
        t = self.t
        cases = {
            "kubectl --context dev -n globex-payroll get pods": f"kubectl {t(9)} {t(3)} {t(2)} {t(14)} {t(3)} {t(4)}",
            "docker run --name dev -e CLIENT=globex-payroll nginx":
                f"docker {t(3)} {t(6)} {t(3)} {t(2)} {t(21)} {t(5)}",
            "cd ~/w && pnpm dev --open --to globex-payroll": f"cd {t(3)} && pnpm {t(3)} {t(6)} {t(4)} {t(14)}",
            "ssh dev -t tmux attach -t globex-payroll": f"ssh {t(3)} {t(2)} {t(4)} {t(6)} {t(2)} {t(14)}",
            "echo dev crew show globex-payroll": f"echo {t(3)} {t(4)} {t(4)} {t(14)}",
            "cat notes/dev": f"cat {t(9)}",
            "sudo -u dev kubectl get ns globex-payroll": f"sudo -u {t(3)} kubectl {t(3)} {t(2)} {t(14)}",
            "grep -rn git log globex-payroll": f"grep {t(3)} {t(3)} {t(3)} {t(14)}",
            "npm run gh -- --repo globex-payroll": f"npm {t(3)} {t(2)} {t(2)} {t(6)} {t(14)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        audit.record("guard.irreversible", "?", "kubectl --context dev delete ns globex-payroll", "deny", tier=2)
        self.assertEqual(audit.query(action="guard.irreversible")[-1]["detail"],
                         f"kubectl {t(9)} {t(3)} {t(6)} {t(2)} {t(14)}")
        self.assertNotIn("globex", self.stored())
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
        # H1: an old guard.deny's detail is the refusal the agent read, and it quotes the irreversible command's words
        # in backticks; the record masks each quoted command (the records of earlier versions stay readable)
        words = "gh pr merge 12 --squash --body SECRET release notes for Acme"
        reason = (f"`{words}` merges PR #12 of acme/app, which cannot be undone (tier 2): the developer approves it "
                  f"first.")
        audit.record("guard.deny", 'gh pr merge 12 --squash --body "SECRET release notes for Acme"', reason[:400],
                     "deny", tool="Bash")
        detail = audit.query(action="guard.deny")[-1]["detail"]
        self.assertTrue(detail.startswith(f"`gh pr merge 12 --squash --body {self.t(29)}` merges PR #12"), detail)
        # an irreversible command the guard cannot parse: its words are the shell text the agent ran
        audit.record("guard.irreversible", "?", "cat <<'EOF' | gh pr merge 12 --body-file -\nSECRET notes\nEOF", "deny",
                     irreversible="unparsed", tier=2)
        self.assertEqual(audit.query(action="guard.irreversible")[-1]["detail"],
                         f"cat <<'EOF' | gh pr merge 12 --body-file -\n{self.t(12)}\nEOF")
        # a record that does not say what it holds is masked as words; only the gate's own errors are kept as they are
        audit.record("guard.irreversible", "#12", "gh pr merge 12 --body SECRET notes", "deny")
        audit.record("guard.irreversible", "", "RuntimeError: state busy", "deny", rule="gate-error")
        self.assertEqual([r["detail"] for r in audit.query(action="guard.irreversible")[-2:]],
                         [f"gh pr merge 12 --body {self.t(12)}", "RuntimeError: state busy"])
        for text in ("SECRET", "Acme"):
            self.assertNotIn(text, self.stored())
        self.unmarked()

    def test_a_guard_detail_keeps_devkits_own_wording(self):
        # a refusal also quotes devkit's own words. A quoted span is read as a shell line, so
        # its operators stay, and `…` stays; a one-word span stays only as a program on devkit's list (a project
        # id is the developer's name too). A quoted command holds the words joined by
        # spaces, so once one is free text only known option names stay after it. The rule reads only the detail,
        # so the stored detail reads back as it was stored
        t, m = self.t, self.m
        kept = ["Run `dev progress …` and `dev crew …` as a command of its own (no `&&`, `;`, pipes, `$(…)`, "
                "`<(…)`, `bash -c`, `env` or `timeout` around it); quote it: `dev crew ask \"…\"`.",
                "read-only lane: `cat` is not on the read-only allowlist."]
        masked = {"`cd ~/x && dev crew ask \"SECRET plan\"` is refused": f"`cd {t(3)} && dev crew ask {m(11)}` is refused",
                  "`gh pr merge 12 --squash --body SECRET notes --head acme-x --acme-merger` merges PR #12":
                      f"`gh pr merge 12 --squash --body {t(12)} --head {t(20)}` merges PR #12",
                  "`Ana Smith` is no crew member; `SECRET-plan` is unknown":
                      f"`{t(9)}` is no crew member; `{t(11)}` is unknown",
                  "This session works on `other-org/devkit`; it must not change "
                  "`fireflysoftwaresolutions/devkit`'s handoff note.":
                      # a path keeps its base name only when devkit's list has it
                      f"This session works on `{t(9)}/devkit`; it must not change `{t(24)}/devkit`'s handoff note."}
        for detail in kept + list(masked):
            audit.record("guard.deny", "Bash", detail, "deny", tool="Bash")
        details = [r["detail"] for r in audit.query(action="guard.deny")]
        self.assertEqual(details, kept + list(masked.values()))
        for text in ("SECRET", "Ana", "acme"):
            self.assertNotIn(text, self.stored())
        self.unmarked()

    def test_a_program_keeps_its_name_only_when_devkits_list_has_it(self):
        # a lowercase word at command position can be a client's
        # name, a file or a password, so a program's name stays only from devkit's fixed list (audit.PROGRAMS), and
        # a program run by its path keeps its base name only when the list has it. 2a: a name written with quotes
        # or an escape stays in neither case
        t = self.t
        cases = {
            "globex-payroll.acme-merger-q3 --x": f"{t(29)} {t(3)}",
            "john.smith --x": f"{t(10)} {t(3)}",
            "hunter2 --x": f"{t(7)} {t(3)}",
            "sudo globex-payroll": f"sudo {t(14)}",
            "timeout 5 globex-payroll": f"timeout 5 {t(14)}",
            "acme-layoffs-list-2026.csv": t(26),
            "CLIENT=globex globex-payroll": f"CLIENT={t(6)} {t(14)}",
            "cat x | acme-merger-tool": f"cat {t(1)} | {t(16)}",
            "./clients/globex/run.sh x": f"{t(23)} {t(1)}",
            "/opt/globex-payroll/bin/python3 x.py": f"{t(23)}/python3 {t(4)}",
            '"globex-payroll" --flag': f"{t(14)} {t(6)}",
            '"cat" notes': f"{t(3)} {t(5)}",
            "kubectl get pods": f"kubectl {t(3)} {t(4)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("globex", "acme", "john", "hunter2", "payroll", "layoffs", "clients"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_the_program_list_is_the_same_on_every_machine(self):
        # the list is fixed, so a program on this machine's PATH changes nothing; it holds the wrappers and the
        # programs the masker reads
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / "globex-payroll"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(audit.mask_command("globex-payroll x"), f"{self.t(14)} {self.t(1)}")
        known = {k.split()[0] for k in audit._WRAPPERS} | set(audit._OTHER) | {"dev", "devkit", "eval"}
        self.assertEqual(sorted(known - audit.PROGRAMS), [])

    def test_a_program_run_by_its_path_keeps_only_its_base_name(self):
        # dev, git, gh, a wrapper, a shell or python run by its path kept the folder, which
        # can name a client; the folder becomes its length. A program run by a quoted path is no program devkit
        # knows, so the line is masked like another program's
        t, m = self.t, self.m
        cases = {
            '/opt/globex-payroll/bin/dev crew ask "SECRET plan"': f"{t(23)}/dev crew ask {m(11)}",
            '/srv/acme-merger/bin/git commit -m "SECRET plan"': f"{t(20)}/git commit -m {t(11)}",
            "/opt/acme/bin/gh pr merge 12 --squash": f"{t(13)}/gh pr merge 12 --squash",
            "/opt/globex/bin/sudo -u root dev sync": f"{t(15)}/sudo -u {t(4)} dev sync",
            "/opt/globex/bin/python3 -m devkit sync": f"{t(15)}/python3 -m devkit sync",
            '/opt/globex/bin/bash -c "dev sync"': f'{t(15)}/bash -c "dev sync"',
            '"/opt/globex payroll/bin/dev" crew ask x': f"{t(27)} {t(4)} {t(3)} {t(1)}",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("globex", "acme", "SECRET"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_an_option_before_the_program_keeps_its_name_and_only_a_value_that_names_nothing(self):
        # nice's -10 and xargs' {} name nothing and stay; a value attached to an option of
        # python or a shell before the program (a path) is masked
        t = self.t
        cases = {
            'nice -10 dev crew ask "SECRET"': f"nice -10 dev crew ask {self.m(6)}",
            'xargs -I{} dev crew ask "SECRET {}"': f"xargs -I{{}} dev crew ask {self.m(9)}",
            "xargs -I {} dev sync": "xargs -I {} dev sync",
            "python3 -X/opt/globex -m devkit sync": f"python3 -X{t(11)} -m devkit sync",
            "bash --rcfile=/opt/globex/rc -c 'dev sync'": f"bash --rcfile={t(14)} -c 'dev sync'",
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        # a shell's option before its -c keeps its decision in a quote, so the record reads back as it was stored
        audit.record("guard.deny", "Bash", "Run `xargs eval bash -I{} -c sudo _local/x` now", "deny", tool="Bash")
        for words in ("globex", "SECRET", "_local/x"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_long_option_a_shell_does_not_take_is_recorded_as_its_length_before_its_c(self):
        # `bash --acme-secret -c …` kept the made-up option in clear: only the shells' own long options keep their
        # name; another one keeps its dashes, so the record still reads back as a shell's -c line
        t = self.t
        cases = {
            'bash --acme-secret -c "dev sync"': f'bash --{t(11)} -c "dev sync"',
            'zsh --globex --login -c "dev sync"': f'zsh --{t(6)} --login -c "dev sync"',
            'bash --norc --noprofile -c "dev sync"': 'bash --norc --noprofile -c "dev sync"',
        }
        for cmd, kept in cases.items():
            with self.subTest(cmd=cmd[:24]):
                self.assertEqual(self.denied(cmd, tool="Bash"), kept)
        for words in ("acme", "globex"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_quote_keeps_a_word_only_when_devkits_list_has_it(self):
        # a refusal's one-word quote stays only as a program on the list; a quote whose first word is not on it
        # is free text, recorded as its length; a path keeps its base name only when the list has it
        t = self.t
        cases = {"`john` takes only a, b": f"`{t(4)}` takes only a, b",
                 "Crew members never run `globex-payroll`: it runs a program": f"Crew members never run `{t(14)}`: it "
                                                                                "runs a program",
                 "Crew members never run `acme payroll cuts`: it runs a program": f"Crew members never run `{t(17)}`: "
                                                                                   "it runs a program",
                 "the sed command `s/john.smith/x/w out` can write": f"the sed command `{t(20)}` can write",
                 "the sed command `s/globex payroll/acme/w out` can write": f"the sed command `{t(27)}` can write",
                 "Branch `feat/globex-payroll-q3` does not follow": f"Branch `{t(22)}` does not follow",
                 "`clients/globex/payroll/q3/salaries` names a path outside": f"`{t(34)}` names a path outside",
                 "`sed` and `dev` and `/usr/bin/git` run": f"`sed` and `dev` and `{t(8)}/git` run"}
        for detail in cases:
            audit.record("guard.deny", "Bash", detail, "deny", tool="Bash")
        self.assertEqual([r["detail"] for r in audit.query(action="guard.deny")], list(cases.values()))
        for words in ("john", "globex", "acme", "payroll", "clients"):
            self.assertNotIn(words, self.stored())
        self.unmarked()

    def test_a_value_with_a_backtick_of_its_own_cannot_end_the_quote_early(self):
        # a refusal quoted a value in single backticks, so a backtick inside it ended the
        # quote and the rest stayed as prose. Later refusals fenced the value so it reads as one quote, and a record
        # written before (prose that touches a quote on both sides, as in `a`b`c`) masks that prose too
        values = [("``--out reports/`cat globex-payroll-q3.txt`/x``", "--out reports/`cat globex-payroll-q3.txt`/x"),
                  ("`` --url http://localhost/`echo acme merger secret` ``",
                   "--url http://localhost/`echo acme merger secret`")]
        for fenced, shown in values:
            msg = (f"{fenced} has a character the shell expands ($, a backtick, {{, *, ? or [), so the engine could "
                   f"get another value than the one devkit read: write the value out in full")
            audit.record("guard.deny", "Bash", msg, "deny", tool="Bash")
            audit.record("guard.deny", "Bash", f"`{shown}` has a character the shell expands", "deny", tool="Bash")
        audit.record("guard.deny", "Bash", "`--name a`b`c globex` has a character the shell expands", "deny", tool="Bash")
        details = [r["detail"] for r in audit.query(action="guard.deny")]
        self.assertTrue(details[0].startswith(f"``{self.t(43)}`` has a character the shell expands"), details[0])
        self.assertEqual(details[-1], f"`{self.t(8)}`{self.t(1)}`{self.t(8)}` has a character the shell expands")
        for words in ("globex", "payroll", "cat ", "echo", "acme", "merger", "secret", "reports", "localhost"):
            self.assertNotIn(words, self.stored())
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
                          (f"rm {self.t(3)} {self.t(3)}", "refused"),
                          ("main", "dev crew ask [message text not recorded: 22 characters] (no result within 120 s)"),
                          (f"add {self.t(22)} --why {self.t(9)}", "")])
        self.assertEqual([(r["target"], r["detail"]) for r in new], [(r["target"], r["detail"]) for r in sealed])
        stored_new = [json.loads(ln) for ln in self.stored().splitlines()][-4:]
        self.assertEqual(new, stored_new)  # a record the current rules wrote reads back as stored, unmarked
        self.assertNotIn("secret plan", self.stored().split("\n", 4)[-1])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_dev_visual_keeps_its_ids_and_settings_but_no_url_selector_step_or_page_text(self):
        # a page's URL can carry a token or a person's name, a selector and a recipe step hold the page's own text
        # (fill #email jane@…), and a state or a label is what the page shows: each is masked; record ids, map ids,
        # the matrix (viewport, theme) and paths stay
        t = self.t
        cases = [
            (["capture", "https://app.example.com/customers/jane-doe?code=abc123", "--do", "fill #email jane@example.com",
              "--state", "list, October", "--css", ".total{color:red}", "--viewport", "1440x900", "--theme", "dark",
              "--audit", "VA-g33y7d2p2s", "-p", "acme/app"],
             f"capture {t(54)} --do {t(28)} --state {t(13)} --css {t(17)} --viewport 1440x900 --theme dark "
             f"--audit VA-g33y7d2p2s -p acme/app"),
            (["component", "https://app.example.com/story?id=pay", "--element", "#pay-jane", "--states", "default,hover",
              "--state-do", "hover=hover #pay-jane", "--mask", ".avatar", "--data", "Jane's demo seed"],
             f"component {t(36)} --element {t(9)} --states default,hover --state-do {t(21)} --mask {t(7)} "
             f"--data {t(16)}"),
            (["finding", "add", "VC-k2m4n6p8q0", "--selector", 'role=button[name="Pay Jane"]', "--title",
              "Pay button hidden for Jane", "--severity", "P1", "--node", "invoices.list#empty", "--rule", "wcag:2.5.8"],
             f"finding add VC-k2m4n6p8q0 --selector {t(28)} --title {t(26)} --severity P1 --node invoices.list#empty "
             f"--rule wcag:2.5.8"),
            (["map", "crawl", "https://staging.example.com/", "--within", "/customers/jane", "--avoid", "Jane",
              "--exclude", "/admin", "--max", "30", "--storage-state", ".auth/staging.json"],
             f"map crawl {t(28)} --within {t(15)} --avoid {t(4)} --exclude {t(6)} --max 30 "
             f"--storage-state .auth/staging.json"),
            (["journey", "walk", "J:export", "--base", "https://staging.example.com/?invite=jane", "--state",
              "Jane's invoices", "--clock", "2026-10-15T09:00:00Z"],
             f"journey walk J:export --base {t(40)} --state {t(15)} --clock 2026-10-15T09:00:00Z"),
            (["map", "journey", "add", "J:export", "--path", "home>invoices.list", "--actor", "Jane Doe", "--task",
              "export Jane's invoices", "--decision", "invoices.list: Jane has none", "--keeps", "October",
              "--budget", "6"],
             f"map journey add J:export --path home>invoices.list --actor {t(8)} --task {t(22)} --decision {t(28)} "
             f"--keeps {t(7)} --budget 6"),
            (["map", "concept", "add", "Invoice", "--label", "Jane's bills", "--relates", "Customer:belongs to Jane",
              "--why", "Jane asked"],
             f"map concept add Invoice --label {t(12)} --relates {t(24)} --why {t(10)}"),
            (["start", "Can Jane export October?", "--scope", "screen", "--viewport", "1440x900", "--theme", "light"],
             f"start {t(24)} --scope screen --viewport 1440x900 --theme light"),
        ]
        for argv, kept in cases:
            with self.subTest(argv=argv[:2]):
                audit.record("dev.visual", argv, "", "ok")  # cli.py passes dev's arguments as a list
                self.assertEqual(audit.query(action="dev.visual")[-1]["target"], kept)
        self.unmarked()
        for words in ("Jane", "jane", "October", "example.com", "customers", "avatar", "color:red"):
            self.assertNotIn(words, self.stored())

    def test_an_option_name_takes_a_value_where_its_command_reads_one_and_stays_a_flag_elsewhere(self):
        # dev visual reads --decision as a value (a decision id) where dev crew ask reads it as a flag: a question
        # typed after the flag is crew ask's message, never the flag's value
        cases = [
            ("visual", ["finding", "close", "VF-k2m4n6p8q0", "--as", "duplicate", "--of", "VF-a1b2c3d4e5",
                        "--decision", "D3", "--why", "Jane said so"],
             f"finding close VF-k2m4n6p8q0 --as duplicate --of VF-a1b2c3d4e5 --decision D3 --why {self.t(12)}"),
            ("crew", ["ask", "--decision", "Acme?", "--to", "tank"], f"ask --decision {self.m(5)} --to tank"),
        ]
        for cmd, argv, kept in cases:
            with self.subTest(argv=argv[:2]):
                audit.record("dev." + cmd, argv, "", "ok")
                self.assertEqual(audit.query(action="dev." + cmd)[-1]["target"], kept)
        for words in ("Jane", "Acme"):
            self.assertNotIn(words, self.stored())

    def test_every_dev_command_positional_and_valued_option_is_classified(self):
        # M2: the tables follow dev's parser. Every command is listed with each positional, in the parser's order,
        # with its nargs and a class (text, name, select or pair); every option that takes a value is free text or
        # names something; and a NAME option a command reads as a flag is listed for that command. A new command,
        # positional or valued option fails here until it is classified. Whether a class is right is not checked
        # here: the masking tests above check the classes that hold text. FLAG_OPTIONS lists the options that take
        # no value, the rest of the names dev's parser knows. A NAME option a command reads as free text (NAME_TEXT)
        # is one that command takes with a value
        import argparse
        from devkit import cli
        valued, bare, commands, flags, valued_at = set(), set(), {}, {}, {}

        def walk(p, path):
            here = []
            for act in p._actions:
                if isinstance(act, argparse._SubParsersAction):
                    for name, sub in act.choices.items():
                        walk(sub, path + [name])
                elif not act.option_strings:
                    here.append((act.dest, {None: "", argparse.REMAINDER: "..."}.get(act.nargs, act.nargs)))
                elif isinstance(act, argparse._HelpAction):
                    bare.update(act.option_strings)
                else:
                    (valued if act.nargs != 0 else bare).update(act.option_strings)
                    if act.nargs != 0:
                        valued_at.setdefault(" ".join(path), set()).update(act.option_strings)
                    if act.nargs in (0, "?", "*"):
                        flags.setdefault(" ".join(path), set()).update(o for o in act.option_strings
                                                                       if o in audit.NAME_OPTIONS)
            commands[" ".join(path)] = here
        walk(cli.build_parser(), [])
        self.assertEqual(sorted(valued - audit.TEXT_OPTIONS - audit.NAME_OPTIONS), [])
        self.assertEqual(sorted(audit.TEXT_OPTIONS & audit.NAME_OPTIONS), [])
        self.assertEqual(sorted(audit.FLAG_OPTIONS), sorted(bare - audit.TEXT_OPTIONS - audit.NAME_OPTIONS))
        self.assertEqual(sorted(set(commands) ^ set(audit.COMMAND_ARGS)), [])
        for path, positionals in commands.items():
            with self.subTest(command=path):
                slots = audit.command_slots(path)
                self.assertEqual([(dest, nargs) for dest, _, nargs in slots], positionals)
                self.assertEqual([c for _, c, _ in slots if c not in audit.ARG_CLASSES], [])
        self.assertEqual({k: v for k, v in flags.items() if v}, {k: set(v) for k, v in audit.NAME_FLAGS.items()})
        self.assertEqual({k: sorted(v - (valued_at.get(k, set()) & audit.NAME_OPTIONS))
                          for k, v in audit.NAME_TEXT.items() if v - (valued_at.get(k, set()) & audit.NAME_OPTIONS)},
                         {})


class QuoteTests(unittest.TestCase):
    # The trail reads a quote in a detail as a Markdown code span: a value devkit quoted with shellparse.code_span,
    # backticks of its own included, at either end or in runs, is one quote whose text is the value, and the prose
    # around it stays prose. Records of earlier versions quote this way too.

    def test_a_value_quoted_by_devkit_reads_back_as_one_quote(self):
        from devkit import shellparse
        for value in ("plain words", "a`b", "`a", "a`", "a``b`c", "```", "reports/`cat globex`/x", " a ", ""):
            with self.subTest(value=value):
                parts = audit._quotes(f"Run {shellparse.code_span(value)} now: {shellparse.code_span('dev')}.")
                self.assertEqual([p[0] for p in parts], ["prose", "quote", "prose", "quote", "prose"])
                body = parts[1][2]  # Markdown takes one space off each end when both have one
                read = body[1:-1] if body[:1] == body[-1:] == " " and body.strip(" ") else body
                self.assertEqual(read, value or " ")  # an empty value is a span of one space
                self.assertEqual([parts[0][1], parts[2][1], parts[3][2], parts[4][1]], ["Run ", " now: ", "dev", "."])

    def test_a_sed_script_quoted_with_a_backtick_is_one_quote_and_masked_in_a_detail(self):
        from devkit import readonly
        why = readonly._ro_sed(["-n", "s/x/`cat globex-payroll.txt`/w out", "notes.md"])
        quotes = [p for p in audit._quotes(why) if p[0] == "quote"]
        self.assertEqual([q[2].strip(" ") for q in quotes], ["s/x/`cat globex-payroll.txt`/w out"])
        self.assertNotIn("globex", audit._field("guard.deny", "detail", why, []))


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
        # the test sets the file mode it checks, on its own temporary files
        os.chmod(audit.AUDIT, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
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
        # last month's records, so their days are closed and publish; the records publish() writes go elsewhere
        ym = audit.months_back(time.time(), 1)
        month = audit.AUDIT / f"{ym}.jsonl"
        noon = calendar.timegm((int(ym[:4]), int(ym[5:]), 14, 12, 0, 0, 0, 0, 0))
        for i in range(3):
            audit._append(audit.build("dev.task", f"add {i}", now=noon + i))
        published = audit.team_dir() / ym / f"{ym}-14.jsonl"
        self.assertEqual(audit.publish(), [published])
        for i in range(2):
            audit._append(audit.build("dev.task", f"later {i}", now=noon + 86400 + i))
        res = audit.verify()
        self.assertTrue(res["ok"])
        self.assertEqual(res["unpublished"], 2)  # chained, but not yet anchored: reported, never called verified
        lines = month.read_text().splitlines()
        month.write_text("\n".join(lines[:2]) + "\n")  # the local file cut below the published head
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual(res["first"]["kind"], "diverged")
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

    def test_team_months_prune_this_macs_copies_under_its_former_hostnames_and_never_another_macs(self):
        from devkit import identity
        jan, jan_end = time.mktime((2025, 1, 10, 12, 0, 0, 0, 0, -1)), time.mktime((2025, 1, 31, 12, 0, 0, 0, 0, -1))
        # this Mac carried mac-158 all January 2025; another Mac carried mac-55 then, this one only since October
        m = identity.Machine("m-0000aaaa", "andress-mbp-2",
                             {"mac-158": [[jan - 86400, jan_end + 86400]], "mac-55": [[NOV6 - 30 * 86400, NOV6]]})
        core.CFG["audit"]["retention"]["team_months"] = 6
        with mock.patch.object(identity, "machine", return_value=m):
            user = audit.actor()["user"]
            for host in ("mac-158", "mac-55"):
                d = core.HANDOFF / "audit" / f"{user}@{host}"
                d.mkdir(parents=True)
                for st, ts in (("2025-01", jan_end), ("2026-10", NOV6 - 20 * 86400)):
                    (d / f"{st}.jsonl").write_text(json.dumps({"ts": ts, "actor": {"user": user, "host": host}}) + "\n")
            out = audit.prune(now=NOV6)
        self.assertEqual(sorted(str(p.relative_to(core.HANDOFF)) for p in out["team"]),
                         [f"audit/{user}@mac-158/2025-01.jsonl"])
        self.assertTrue((core.HANDOFF / "audit" / f"{user}@mac-55" / "2025-01.jsonl").exists())  # another Mac's
        self.assertTrue((core.HANDOFF / "audit" / f"{user}@mac-158" / "2026-10.jsonl").exists())  # within 6 months

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
        self.assertFalse(audit.audited(["hook", "pre-tool"]))

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

    def test_records_never_carry_the_hostname_and_this_macs_siem_export_finds_it_from_the_machine_id(self):
        from devkit import identity
        m = identity.Machine("m-0000aaaa", "andress-mbp-2", {"andress-mbp-2": [[0, NOV6]]})
        with mock.patch.object(identity, "machine", return_value=m):
            self.assertEqual(audit.actor()["host"], "m-0000aaaa")
            self.assertNotIn("label", audit.actor())  # records are published: they never carry the hostname
            audit.record("dev.task", "done 9")
            r = audit.query(action="dev.task")[-1]
            self.assertNotIn("andress", json.dumps(r))
            # this Mac's own export finds the hostname it carried then from the machine id
            dev = audit.to_ocsf(r)["device"]
            self.assertEqual((dev["hostname"], dev["uid"]), ("andress-mbp-2", "m-0000aaaa"))
            self.assertIn("shost=andress-mbp-2", audit.to_cef(r))
            self.assertIn("deviceExternalId=m-0000aaaa", audit.to_cef(r))
            self.assertIn(b" andress-mbp-2 devkit: ", audit._syslog_line(r))
            # another Mac's record is named by its machine id
            theirs = dict(r, actor={"user": "bob", "host": "m-0000bbbb", "agent": "codex", "session": ""})
            self.assertEqual(audit.to_ocsf(theirs)["device"], {"hostname": "m-0000bbbb", "uid": "m-0000bbbb",
                                                                "type_id": 0})
        # a record from before the machine id names the host only: it stays the hostname, with no device id
        old = dict(r, actor={"user": "ana", "host": "mac-158", "agent": "claude", "session": ""})
        self.assertEqual(audit.to_ocsf(old)["device"], {"hostname": "mac-158", "type_id": 0})
        self.assertIn("shost=mac-158", audit.to_cef(old))
        self.assertNotIn("deviceExternalId", audit.to_cef(old))

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


class LongConfigDetailTests(unittest.TestCase):
    # Any teammate can write a dev.config.set line into a handoff copy, and trying every arrow in a long detail made
    # one team query take seconds: each try parses the text before the arrow. The tries are few, whatever the length
    def test_the_parses_of_a_detail_do_not_grow_with_its_arrows(self):
        shapes = {"an open string": lambda n: '"' + "a → " * n, "a list of strings": lambda n: "[" + '"a → ", ' * n}
        for name, shape in shapes.items():
            counts = []
            for n in (100, 32000):
                with mock.patch.object(audit, "_json", wraps=audit._json) as parse:
                    self.assertEqual(audit._config_value(shape(n)), "a value it does not quote")
                counts.append(parse.call_count)
            self.assertEqual(counts[0], counts[1], name)

    def test_an_old_detail_still_says_its_new_value_however_long_its_old_value(self):
        # devkit cut no detail before schema v2, so an old record can hold a long value whole
        self.assertEqual(audit._config_value('null → "x"'), core.say_value("x"))
        self.assertEqual(audit._config_value('"' + "b" * 5000 + '" → "x"'), core.say_value("x"))
        self.assertEqual(audit._config_value('"a → b" → "x"'), core.say_value("x"))


class NotUtf8RecordTests(AuditCase):
    # An argument's bytes that are not UTF-8 reach Python as lone surrogates (os.fsdecode), which strict UTF-8 cannot
    # write: record() dropped the whole record, so one such argument kept a dev rm out of the audit trail
    def stored(self) -> list:
        return [json.loads(ln) for f in sorted(audit.AUDIT.glob("*.jsonl"))
                for ln in f.read_bytes().decode("utf-8").splitlines() if ln.strip()]

    def test_a_record_with_bytes_that_are_not_utf8_is_written_and_its_chain_verifies(self):
        cases = {"an argument": dict(target=["x\udce9", "secrets.txt"]), "the detail": dict(detail="caf\udce9"),
                 "the why": dict(why="caf\udce9"), "the folder": dict(cwd="/tmp/caf\udce9")}
        for name, fields in cases.items():
            with self.subTest(name):
                rec = audit.record("dev.rm", **fields)
                self.assertIsNotNone(rec)
                self.assertIn(rec["id"], [r.get("id") for r in self.stored()])
        self.assertTrue(audit.verify()["ok"])

    def test_a_record_with_escapes_reads_and_publishes_as_it_was_written(self):
        # an escape is six characters for one byte, so a nested value outgrew its cut, and an escape can read as more
        # than its byte (\udcda then "pi_key=" reads as api_key=): build writes what a reader sees, nothing it cleans
        audit.record("dev.rm", "", "\udcdapi_key=hunter2", cwd="/tmp/" + "caf\udce9/" * 120)
        ln = audit.AUDIT.joinpath(f"{audit.months_back(time.time(), 0)}.jsonl").read_bytes().splitlines()[-1]
        rx = audit._redact_rules()
        view = audit.lift(json.loads(ln), ln, rx)
        self.assertNotIn("cleaned", view)
        self.assertLessEqual(len(view["evidence"]["cwd"]), audit.VALUE_CUT)
        self.assertNotIn(b"hunter2", ln)
        self.assertEqual(audit._published_line(ln, rx), ln)

    def test_a_hand_edited_v2_line_holding_a_lone_surrogate_reads_as_cleaned(self):
        rec = audit.build("dev.task", "t", "", now=time.time())
        rec["why"] = "x\ud800"
        ln = json.dumps(rec, separators=(",", ":")).encode()
        self.assertTrue(audit.lift(json.loads(ln), ln, audit._redact_rules()).get("cleaned"))

    def test_the_bytes_stay_readable_as_their_escape(self):
        audit.record("dev.rm", "", "caf\udce9")
        self.assertEqual(self.stored()[-1]["detail"], "caf\\udce9")


class OldJobRecordTests(AuditCase):
    # Records an older devkit wrote kept a typed configuration key or a job's name whole: a dev.config.set target, a
    # jobs.set target, the jobs.install list and a job.<name> action. A token pasted as a job's or profile's name must
    # not come back from them: a reader says each one as a new record does (core.say_config_key, jobs.said).
    TOKEN = "".join(("q7rk2plm", "9zt4wv8b", "n3hc6xd5"))  # in pieces: a fixture, not a credential to scan

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(core, "CONFIG_FILE", self.root / "config.json")  # the key rule reads no real file
        patcher.start()
        self.addCleanup(patcher.stop)
        tok = self.TOKEN
        audit.record("jobs.set", "probe", "{}")
        f = next(audit.AUDIT.glob("*.jsonl"))
        base = json.loads(f.read_text().splitlines()[-1])
        old = [("dev.config.set", f"jobs.{tok}.every", json.dumps({"value_type": "int", "value_digest": "ab" * 32})),
               ("dev.config.set", f"codex.profiles.{tok}.model", "{}"),
               ("jobs.set", tok, json.dumps({"cmd": ["status", tok], "about": tok, "every": 30})),
               ("jobs.install", f"sync,{tok}", f"ok:sync; loaded:{tok}"),
               (f"job.{tok}", "status", "fine")]
        lines = [dict(base, action=a, target=t, detail=d, id=f"{base['id']}{i}") for i, (a, t, d) in enumerate(old)]
        v1 = {"ts": base["ts"], "user": "u", "host": "h", "agent": "", "action": f"job.{tok}", "target": "status",
              "detail": "", "outcome": "ok"}
        with f.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in lines + [v1]))
        self.assertIn(tok, f.read_text())  # stored as the older devkit wrote it

    def test_no_query_or_export_format_shows_a_token_from_an_old_record(self):
        recs = audit.query(days=1)
        self.assertEqual(len(recs), 7)
        self.assertNotIn(self.TOKEN, json.dumps(recs))
        for fmt in audit.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertNotIn(self.TOKEN, audit.export(fmt, days=1))
        said = jobs.said(self.TOKEN)
        self.assertRegex(said, r"\A<key of 24 characters>#[0-9a-f]{8}\Z")
        got = {(r["action"], r["target"], r["detail"]) for r in recs}
        self.assertTrue({("dev.config.set", "jobs.<key of 24 characters>.every", "a number"),
                         ("dev.config.set", "codex.profiles.<key of 24 characters>.model",
                          "a value it does not quote"),
                         ("jobs.set", said, json.dumps({"every": 30})),
                         ("jobs.install", f"sync,{said}", f"ok:sync; loaded:{said}"),
                         (f"job.{said}", "status", "fine")} <= got, got)
        self.assertIn(("jobs.set", "probe", "{}"), got)  # a short word stays, as a new record keeps it
        self.assertNotIn("ab" * 32, json.dumps(recs))  # a value's digest confirms a guessed value

    def test_an_old_value_change_reads_only_as_the_new_values_type(self):
        # devkit 26.11.3 to 26.11.18 recorded a dev.config.set value as "<old JSON> → <new JSON>", both values whole;
        # a reader says the new value as a new record does (core.say_value), and a detail it cannot tell is one of
        # devkit's own shapes as "a value it does not quote"
        pw = self.TOKEN
        f = next(audit.AUDIT.glob("*.jsonl"))
        base = json.loads(f.read_text().splitlines()[0])
        cases = [(f"null → {json.dumps(['deploy', '--password', pw])}", "a list of 3 items"),
                 (f'"was → {pw}" → "{pw}"', "a string of 24 characters"),  # an arrow inside a value
                 ("4 → 6", "a number"),
                 ('{"a": 1} → null', "null"),
                 (f'null → "{pw}{"x" * 400}"'[:300], "a value it does not quote"),  # cut at 300 characters
                 (f"rotated {pw}", "a value it does not quote"),
                 ("a string of 40 characters", "a string of 40 characters"),
                 ("an object of 1 key", "an object of 1 key"),
                 ("a string", "a string"),
                 ("validated editor candidate", "validated editor candidate"),
                 ("", "")]
        lines = [dict(base, action="dev.config.set", target="progress.max_parallel", detail=d, id=f"{base['id']}c{i}")
                 for i, (d, _) in enumerate(cases)]
        v1 = json.dumps({"ts": base["ts"], "user": "u", "host": "h", "agent": "", "action": "dev.config.set",
                         "target": "progress.max_parallel", "detail": cases[0][0], "outcome": "ok"})
        with f.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in lines) + v1 + "\n")
        self.assertIn(pw, f.read_text())  # stored as the older devkit wrote it
        got = {r["id"]: r["detail"] for r in audit.query(days=1, action="dev.config.set")}
        self.assertEqual([got[r["id"]] for r in lines], [want for _, want in cases])
        self.assertEqual(got[audit._v1_id({}, v1.encode())], "a list of 3 items")
        for fmt in audit.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertNotIn(pw, audit.export(fmt, days=1))

    def test_one_id_finds_a_jobs_old_and_new_records(self):
        audit.record(f"job.{jobs.said(self.TOKEN)}", "status", "new", "ok")
        said = jobs.said(self.TOKEN)
        self.assertEqual(len(audit.query(days=1, action=f"job.{said}")), 3)  # v2 and v1 old records, and the new one
        self.assertEqual(len(audit.query(days=1, grep=re.escape(said))), 5)
        self.assertEqual(audit.latest(10, days=1, action=f"job.{said}")[0], 3)


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


class FilterBeforeLiftTests(AuditCase):
    # cleaning a record (lift) is most of a query's time: a query checks the time and the action on the stored record
    # and lifts only what passes, with the same result as lifting every record and filtering after
    def write(self, path: Path, recs: list):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def v2(self, rid: str, ts: float, action: str, target: str = "x", **fields) -> dict:
        return {"v": 2, "id": rid, "ts": ts, "actor": {"user": "fixture-user", "host": "mac", "agent": "claude",
                                                       "session": ""},
                "action": action, "category": "change", "severity": "info", "target": target, "detail": "",
                "outcome": "ok", **fields}

    def v1(self, ts, action, target: str = "x", **fields) -> dict:
        r = {"user": "fixture-user", "host": "mac", "agent": "claude", "session": "", "target": target, "detail": "",
             "outcome": "ok", **fields}
        if ts is not None:
            r["ts"] = ts
        if action is not None:
            r["action"] = action
        return r

    def first_copies(self, team=False) -> list:
        # every stored line lifted, in read order, the first copy of an id kept: what the reader saw before any filter
        seen, out = set(), []
        for f in audit._files(team):
            for ln in f.read_bytes().splitlines():
                rec = audit.lift(json.loads(ln), ln)
                if rec["id"] not in seen:
                    seen.add(rec["id"])
                    out.append(rec)
        return out

    @staticmethod
    def keeps(r: dict, lo=None, hi=None, action=None, actor_=None, project=None, member=None, category=None,
              severity=None, outcome=None, grep=None) -> bool:
        # every filter of `dev audit` on a lifted record, as the reader applied them before any check moved before lift
        a, ts, sev = r.get("actor") or {}, r.get("ts") or 0, audit.SEVERITIES.index
        text = " ".join(str(r.get(k) or "") for k in ("action", "target", "detail", "why"))
        return not ((lo and ts < lo) or (hi and ts > hi) or (action and not r["action"].startswith(action))
                    or (actor_ and actor_ not in f"{a.get('user')}@{a.get('host')} {a.get('agent')}")
                    or (project and r.get("project") != project
                        and not str(r.get("project", "")).startswith(project.rstrip("/") + "/"))
                    or (member and (r.get("crew") or {}).get("member") != audit._member(member)[0])
                    or (category and r.get("category") != category)
                    or (severity and sev(r.get("severity", "info")) < sev(severity))
                    or (outcome and r.get("outcome") != audit.outcome_of(outcome)[0])
                    or (grep and not re.search(grep, text, re.I)))

    def view(self, *argv):
        # `dev audit <argv>` through the command, with the records it lifts counted -> (exit code, output, lifts)
        import argparse
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(list(argv))
        out = io.StringIO()
        with mock.patch.object(audit, "lift", wraps=audit.lift) as lift, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = audit.cmd_audit(a)
        return rc, out.getvalue(), lift.call_count

    def lift_then_filter(self, lo=None, hi=None, action=None, team=False) -> list:
        # the reader before this change: every line lifted, the first copy of an id kept, then filtered
        seen, out = set(), []
        for f in audit._files(team):
            for ln in f.read_bytes().splitlines():
                rec = audit.lift(json.loads(ln), ln)
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                ts = rec.get("ts") or 0
                if (lo and ts < lo) or (hi and ts > hi) or (action and not rec["action"].startswith(action)):
                    continue
                out.append(rec)
        return sorted(out, key=lambda r: r.get("ts") or 0)

    def counted(self, **filters):
        with mock.patch.object(audit, "lift", wraps=audit.lift) as lift:
            got = audit.query(**filters)
        return got, lift.call_count

    def test_a_query_lifts_only_the_records_whose_time_and_action_match_and_returns_what_a_full_lift_returns(self):
        now, day = time.time(), 86400
        home = "ls /Users/fixture-user/Development/acme"  # a refused command's path: cleaned on read
        self.write(core.HANDOFF / "audit" / "fixture-user@mac" / "2026-11" / "2026-11-15.jsonl", [
            self.v2("G1", now - day, "guard.deny", home),  # also in this Mac's file: read once
            self.v2("G4", now - 20 * day, "guard.deny")])  # differs from the local line: the first copy read decides
        self.write(audit.AUDIT / "2026-11.jsonl", [
            self.v2("G1", now - day, "guard.deny", home),
            self.v2("G2", now - 10 * day, "guard.deny"),
            self.v2("T1", now - 3600, "dev.task"),
            self.v2("G3", now - 7200, "guard.ask", "git push --force"),
            self.v2("G4", now - day, "guard.deny"),
            self.v1(now - 2 * day, "guard.deny", "git clean -fdx"),
            self.v1(str(now - 3 * day), "guard.deny", "git reset --hard"),  # lift reads a text time as a number
            self.v1(now - day, "dev.push"),
            self.v1(None, "guard.deny"),  # time 0: before any --since, inside any --until
            self.v1(now - 3600, None)])
        got, lifts = self.counted(days=7, action="guard", team=True)
        self.assertEqual([r["target"] for r in got], ["git reset --hard", "git clean -fdx",
                                                      "ls [text not recorded: 36 characters]", "git push --force"])
        self.assertEqual(got, self.lift_then_filter(lo=now - 7 * day, action="guard", team=True))
        self.assertEqual(lifts, 4)  # 10 stored records and 2 published copies; 4 match
        until = time.strftime("%Y-%m-%d", time.localtime(now - 5 * day))
        for filters, oracle in (
                ({"until": until, "action": "guard"}, {"hi": audit.when(until, end=True), "action": "guard"}),
                ({"days": 7}, {"lo": now - 7 * day}),
                ({"action": "dev"}, {"action": "dev"})):
            with self.subTest(**filters):
                got, lifts = self.counted(team=True, **filters)
                self.assertEqual(got, self.lift_then_filter(team=True, **oracle))
                self.assertEqual(lifts, len(got))
        self.assertEqual([r["id"] for r in audit.query(until=until, action="guard", team=True)][-2:], ["G4", "G2"])

    def test_every_filter_combination_returns_a_full_lift_and_lifts_no_v2_record_its_stored_fields_rule_out(self):
        # cleaning never changes a record's category, severity, outcome or actor, so a v2 record is checked on them
        # before it is lifted; it can change the project, the member and the text --grep reads (a home folder becomes
        # ~), so those are checked after. Each filter's value keeps some records here and drops others.
        now, day, home = time.time(), 86400, "/Users/fixture-user"
        codex = {"user": "fixture-user", "host": "mac", "agent": "codex", "session": ""}
        self.write(core.HANDOFF / "audit" / "fixture-user@mac" / "2026-11" / "2026-11-15.jsonl", [
            self.v2("E5", now - day, "dev.push", "origin main", project="~/acme/web", crew={"member": "tank"})])
        self.write(audit.AUDIT / "2026-11.jsonl", [
            self.v2("A1", now - 3600, "guard.deny", "git push --force", category="refusal", severity="warning",
                    outcome="blocked", actor=codex, project="acme/app"),
            self.v2("B2", now - 2 * 3600, "dev.task", f"{home}/Development/acme", project=f"{home}/acme/web",
                    crew={"member": "tank"}),  # its project and target match ~/ only once cleaned
            self.v2("C3", now - 3 * 3600, "guard.ask", "git reset --hard", category="decision", severity="notice",
                    outcome="recorded", actor=codex, project="acme/app/sub", why=f"asked in {home}/acme"),
            self.v2("D4", now - 10 * day, "guard.deny", "git push --force", category="refusal", severity="warning",
                    outcome="blocked", actor=codex, project="acme/app"),
            # the published copy above is read first and decides: this one's blocked outcome and actor never count
            self.v2("E5", now - day, "dev.push", "origin main", severity="critical", outcome="blocked", actor=codex),
            self.v2("F6", now - 4 * 3600, "guard.deny", f"ls {home}/x", category="security", severity="critical",
                    outcome="blocked", actor={**codex, "user": "other-user", "agent": "claude"}),
            self.v1(now - 2 * day, "guard.deny", "git clean -fdx", outcome="deny", agent="codex", project="acme/app"),
            self.v1(str(now - 3 * day), "dev.push", f"{home}/Development/web", outcome="exit 1",
                    project=f"{home}/acme/web", member="Tank"),
            self.v1(now - 20 * day, "guard.deny", "git clean -fdx", outcome="deny", agent="codex"),
            self.v1(now - 5 * 3600, "dev.task", "done 4", agent="codex", why="moved ~/notes")])
        values = {"days": 7, "action": "guard", "actor_": "codex", "project": "~/acme/web", "member": "Tank",
                  "category": "refusal", "severity": "warning", "outcome": "blocked", "grep": "~/"}
        every = self.first_copies(team=True)
        self.assertEqual(len(every), 10)
        for size in range(len(values) + 1):
            for names in itertools.combinations(values, size):
                filters = {k: values[k] for k in names}
                oracle = {k: v for k, v in filters.items() if k != "days"}
                if "days" in filters:
                    oracle["lo"] = now - filters["days"] * day
                window = {k: oracle[k] for k in ("lo", "action") if k in oracle}
                stored = {k: oracle[k] for k in ("actor_", "category", "severity", "outcome") if k in oracle}
                with self.subTest(**filters):
                    got, lifts = self.counted(team=True, **filters)
                    self.assertEqual(got, sorted((r for r in every if self.keeps(r, **oracle)),
                                                 key=lambda r: r.get("ts") or 0))
                    # a v1 record is lifted to read its fields; a v2 record only when its stored fields match
                    self.assertEqual(lifts, sum(self.keeps(r, **window) and (r["v"] == 1 or self.keeps(r, **stored))
                                                for r in every))

    def test_the_table_view_with_only_time_and_action_filters_lifts_only_the_rows_it_shows(self):
        # 40 records over 9 days, a quarter v1 (some with a text time), stored out of time order, five also published
        now, day = time.time(), 86400
        rows = []
        for i in range(40):
            ts = now - 600 - i * 9 * day / 40
            # actions whose target is plain text, not a command line, a goal or a step's name: cleaning turns the home
            # folder into ~ only
            action, target = ("crew.lost", "watch.wait", "peer.sweep")[i % 3], f"row {i:02d} in /Users/fixture-user/acme"
            rows.append(self.v1(str(ts) if i % 8 == 0 else ts, action, target) if i % 4 == 0 else
                        self.v2(f"R{i:02d}", ts, action, target))
        random.Random(7).shuffle(rows)
        self.write(core.HANDOFF / "audit" / "fixture-user@mac" / "2026-11" / "2026-11-15.jsonl", rows[:5])
        self.write(audit.AUDIT / "2026-11.jsonl", rows)
        since = time.strftime("%Y-%m-%d", time.localtime(now - 6 * day))
        until = time.strftime("%Y-%m-%d", time.localtime(now - 2 * day))
        for argv, oracle, team in (
                (["-n", "5"], {"lo": now - 7 * day}, False),
                (["--action", "watch", "-n", "3", "--team"], {"lo": now - 7 * day, "action": "watch"}, True),
                (["--since", since, "--until", until, "-n", "4"],
                 {"lo": audit.when(since), "hi": audit.when(until, end=True)}, False),
                (["--days", "1", "-n", "60"], {"lo": now - day}, False)):  # fewer matches than -n: all of them
            with self.subTest(argv=argv):
                want = sorted((r for r in self.first_copies(team) if self.keeps(r, **oracle)),
                              key=lambda r: r.get("ts") or 0)
                n = int(argv[argv.index("-n") + 1])
                rc, text, lifts = self.view(*argv)
                self.assertEqual(rc, 0)
                self.assertIn(f" · {len(want)} record(s)", text)  # the count is every match, not the rows shown
                self.assertEqual(re.findall(r"row \d\d in ~/acme", text), [r["target"] for r in want[-n:]])
                self.assertEqual(lifts, min(n, len(want)))
        # --json and a filter past the time and the action still read every match
        want = sorted((r for r in self.first_copies() if self.keeps(r, lo=now - 7 * day)), key=lambda r: r["ts"])
        rc, text, lifts = self.view("--json", "-n", "5")
        self.assertEqual((rc, json.loads(text)), (0, want))
        self.assertEqual(lifts, len(want))
        rc, text, _ = self.view("--grep", "row 1", "-n", "2")
        self.assertEqual(re.findall(r"row \d\d", text),
                         [r["target"][:6] for r in want if r["target"].startswith("row 1")][-2:])

    def heavy_month(self):
        # the shape of a heavy month on the developer's Mac: 90,000 records over 30 days, 4% of them v1, a sixth guard
        # records -> (records, records in the last 7 days, guard records in the last 7 days)
        now, day, n = time.time(), 86400, 90000
        inside = 21000  # the last 7 days, an hour clear of the edge so the query's own clock cannot move one across
        recs, match = [], 0
        for i in range(n):
            ts = now - 60 - i * (7 * day - 3600) / inside if i < inside else \
                now - 7 * day - 3600 - (i - inside) * (23 * day) / (n - inside)
            action = ("guard.deny", "dev.crew", "crew.message", "handoff.sync", "plan.revise", "watch.wait")[i % 6]
            target = f"ask --to tank \"check build {i} in /Users/fixture-user/Development/acme\""
            match += i < inside and i % 6 == 0
            recs.append(self.v1(ts, action, target) if i % 25 == 0 else self.v2(f"01GEN{i:08d}", ts, action, target))
        self.write(audit.AUDIT / "2026-11.jsonl", recs)
        return n, inside, match

    def test_the_table_view_of_a_heavy_week_lifts_only_the_rows_it_shows(self):
        # plain `dev audit`: the last 7 days, the newest 60. The time is a report line, not an assertion
        n, inside, _ = self.heavy_month()
        start = time.perf_counter()
        rc, text, lifts = self.view()
        took = time.perf_counter() - start
        sys.stderr.write(f"\ndev audit, the table view of 7 days on a {n:,}-record trail: {took:.2f} s, "
                         f"{lifts:,} records lifted\n")
        self.assertEqual(rc, 0)
        self.assertIn(f" · {inside} record(s)", text)
        self.assertEqual(lifts, 60, f"{lifts:,} records lifted to show 60 of {inside:,}, {took:.2f} s")

    def test_a_week_of_one_action_on_a_heavy_month_lifts_only_that_week_and_action(self):
        # the time is a report line, not an assertion: a slow runner must not fail it; the lift count is
        n, _, match = self.heavy_month()
        start = time.perf_counter()
        got, lifts = self.counted(days=7, action="guard")
        took = time.perf_counter() - start
        sys.stderr.write(f"\naudit query, 7 days of one action on a {n:,}-record trail: {took:.2f} s, "
                         f"{lifts:,} records lifted\n")
        self.assertEqual(len(got), match)
        self.assertEqual(lifts, match, f"{lifts:,} records lifted for {match:,} matches, {took:.2f} s")


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

    def test_config_names_the_commands_that_change_the_settings_without_a_developer_only_claim(self):
        # any caller changes audit.* with dev config set or edit now: the settings say how, never who may
        rc, text = self.run_cmd("config")
        self.assertEqual(rc, 0)
        flat = " ".join(text.split())
        self.assertIn("Change audit.* with dev config set or dev config edit.", flat)
        self.assertNotIn("Only the developer", flat)
        self.assertNotIn("in a terminal", flat)


class PublishLimitTests(AuditCase):
    # GitHub refuses a file over 100 MB, and then every handoff push, so no file over 45 MB is ever published: each
    # month goes out as one file per UTC day, and a single day past the limit is held
    MB = 1024 * 1024

    def month(self) -> Path:
        return audit.AUDIT / time.strftime("%Y-%m.jsonl")

    def grow(self, size: int, at: int = 0, path: Path = None):
        # the month file at `size` bytes: its first `at` bytes (what is already published), a hole, then the rest of
        # its records, so the file still extends its published copy and no test writes tens of megabytes
        path = path or self.month()
        data = path.read_bytes()
        with open(path, "wb") as fh:
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

    def closed_month(self, back: int = 1):
        # a month that has ended here, so each of its days is closed and publishes: (YYYY-MM, its file, noon on the
        # 14th in seconds)
        ym = audit.months_back(time.time(), back)
        return ym, audit.AUDIT / f"{ym}.jsonl", calendar.timegm((int(ym[:4]), int(ym[5:]), 14, 12, 0, 0, 0, 0, 0))

    def test_a_day_over_45_mb_is_held_while_the_days_before_it_stay_and_one_warning_is_recorded(self):
        ym, last, noon = self.closed_month()
        audit._append(audit.build("dev.task", "add 1", now=noon))
        published = audit.team_dir() / ym / f"{ym}-14.jsonl"
        self.assertEqual(audit.publish(), [published])  # the 14th, while small, as every day
        before = published.read_bytes()
        audit._append(audit.build("dev.task", "add 2", now=noon + 86400))
        self.grow(46 * self.MB, at=last.stat().st_size, path=last)  # the 15th alone is now over the limit
        held = audit.team_dir() / ym / f"{ym}-15.held.json"
        self.assertEqual(audit.publish(), [held])
        self.assertEqual(published.read_bytes(), before)
        self.assertFalse((audit.team_dir() / ym / f"{ym}-15.jsonl").exists())
        warn = audit.query(action="audit.publish")
        self.assertEqual(len(warn), 1)
        self.assertEqual((warn[0]["outcome"], warn[0]["severity"]), ("blocked", "warning"))
        self.assertEqual(warn[0]["target"], core.tilde(last))
        self.assertIn(f"{ym}-15 is not published: 46.0 MB", warn[0]["detail"])
        self.assertIn("45.0 MB limit", warn[0]["detail"])

    def test_the_limit_is_45_mb_per_day_inclusive(self):
        ym, f, noon = self.closed_month(1)
        audit._append(audit.build("dev.task", "add 1", now=noon))
        self.grow(45 * self.MB, path=f)
        published = audit.team_dir() / ym / f"{ym}-14.jsonl"
        ym2, f2, noon2 = self.closed_month(2)
        audit._append(audit.build("dev.task", "add 1", now=noon2))
        self.grow(45 * self.MB + 1, path=f2)
        held = audit.team_dir() / ym2 / f"{ym2}-14.held.json"
        self.assertEqual(sorted(audit.publish()), sorted([published, held]))
        hole, rec = f.read_bytes().splitlines(keepends=True)
        stand_in, out = published.read_bytes().splitlines(keepends=True)
        self.assertEqual(out, rec)  # the record, byte for byte
        self.assertEqual(json.loads(stand_in), {"v": 1, "id": audit._v1_id({}, hole.rstrip(b"\n")), "cleaned": True,
                                                "unreadable": True})  # the hole no reader can read: only named
        self.assertEqual(json.loads(held.read_text())["bytes"], 45 * self.MB + 1)

    def test_stats_says_a_month_over_the_limit_waits_for_the_audit_job_then_names_the_day_held(self):
        audit.record("dev.task", "add 1")
        self.assertEqual(json.loads(self.run_cmd("stats", "--json")[1]).get("held"), [])  # while every month fits
        self.grow(46 * self.MB)
        rc, text = self.run_cmd("stats")
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.month().stem} is not published yet: 46.0 MB here, over the 45.0 MB limit", text)
        self.assertIn("one file per UTC day", text)
        self.assertIn("dev jobs run audit", text)
        self.assertNotIn("planned", text)
        held = json.loads(self.run_cmd("stats", "--json")[1])["held"]
        self.assertEqual([(h["month"], h["bytes"]) for h in held], [(self.month().stem, 46 * self.MB)])
        audit.publish()  # today alone holds the 46 MB, so today is the day held
        rc, text = self.run_cmd("stats")
        held = json.loads(self.run_cmd("stats", "--json")[1])["held"]
        self.assertEqual([h["month"] for h in held], [self.month().stem])
        self.assertIn(f"{held[0]['day']} is not published: 46.0 MB that day, over the 45.0 MB limit", text)
        self.assertIn("local trail", text)
        self.assertIn("dev audit verify", text)

    def test_doctor_fails_its_row_for_a_month_over_the_limit_until_the_job_runs_then_for_the_day_held(self):
        # dev doctor prints each row as (group, name, ok, detail, fix)
        audit.record("dev.task", "add 1")
        self.assertEqual([row[2] for row in audit.publish_checks()], [True])
        self.grow(46 * self.MB)
        rows = audit.publish_checks()
        self.assertEqual([row[2] for row in rows], [False])
        self.assertIn(f"{self.month().stem} is not published yet: 46.0 MB here, over the 45.0 MB limit", rows[0][3])
        self.assertIn("dev jobs run audit", rows[0][4])
        audit.publish()
        rows = audit.publish_checks()
        self.assertEqual([row[2] for row in rows], [False])
        self.assertIn("is not published: 46.0 MB that day", rows[0][3])
        self.assertIn("local trail", rows[0][4])
        self.assertIn("dev audit verify", rows[0][4])
        self.assertNotIn("planned", rows[0][4])


class DailyFileTests(AuditCase):
    # each month is published as one file per closed UTC day. The records are last month's, so each of their days is
    # closed and the records publish() writes itself land in another file; where a test needs a day over the size
    # limit, it lowers the limit so that every file stays small
    def setUp(self):
        super().setUp()
        self.ym = audit.months_back(time.time(), 1)
        self.elsewhere = self.root / "elsewhere"

    def day(self, d: int) -> str:
        return f"{self.ym}-{d:02d}"

    def write(self, d: int, n: int, ym: str = None):
        ym = ym or self.ym
        noon = calendar.timegm((int(ym[:4]), int(ym[5:]), d, 12, 0, 0, 0, 0, 0))
        for i in range(n):
            audit._append(audit.build("dev.task", f"{ym}-{d:02d} record {i}", "x" * 500, now=noon + i))

    def month(self) -> Path:
        return audit.AUDIT / f"{self.ym}.jsonl"

    def folder(self) -> Path:
        return audit.team_dir() / self.ym

    def sizes(self) -> dict:
        # bytes per UTC day, from each record's own time
        out = {}
        for ln in self.month().read_bytes().splitlines(keepends=True):
            d = json.loads(ln)["time"][:10]
            out[d] = out.get(d, 0) + len(ln)
        return out

    def limit(self, n: int):
        return mock.patch.object(audit, "PUBLISH_LIMIT", n)

    def daily(self, changed) -> list:
        return [p.name for p in changed if p.parent == self.folder()]

    def publish_three_days(self):
        for d in (14, 15, 16):
            self.write(d, 3)
        return audit.publish()

    def test_a_month_is_published_as_one_file_per_utc_day(self):
        changed = self.publish_three_days()
        days = [self.folder() / f"{self.day(d)}.jsonl" for d in (14, 15, 16)]
        self.assertEqual(changed, days)
        self.assertFalse((audit.team_dir() / self.month().name).exists())  # no one-file copy any more
        self.assertEqual(b"".join(p.read_bytes() for p in days), self.month().read_bytes())  # in order, whole
        for p in days:
            self.assertEqual({json.loads(ln)["time"][:10] for ln in p.read_text().splitlines()}, {p.stem})
        self.assertEqual(audit.publish(), [])  # a published day is written once

    def test_the_open_day_waits_until_a_record_of_a_later_day_follows_it(self):
        ym = time.strftime("%Y-%m")
        self.write(1, 3, ym)
        self.write(2, 3, ym)
        self.assertEqual([p.name for p in audit.publish()], [f"{ym}-01.jsonl"])
        self.write(3, 1, ym)
        self.assertEqual([p.name for p in audit.publish()], [f"{ym}-02.jsonl"])

    def test_another_machine_reads_and_verifies_the_daily_files(self):
        self.publish_three_days()
        ids = sorted(json.loads(ln)["id"] for ln in self.month().read_text().splitlines())
        audit.AUDIT = self.elsewhere  # a machine that holds none of these records
        self.assertEqual(sorted(r["id"] for r in audit.query(team=True)), ids)  # each record once
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual([(Path(f["path"]).name, f["records"]) for f in res["files"]],
                         [(f"{self.day(d)}.jsonl", 3) for d in (14, 15, 16)])

    def test_an_embargo_recorded_only_in_a_published_daily_file_is_still_found(self):
        from devkit import branches
        noon = calendar.timegm((int(self.ym[:4]), int(self.ym[5:]), 14, 12, 0, 0, 0, 0, 0))
        audit._append(audit.build("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok",
                                  {"project": "acme/app", "branch": "fix/unshipped"}, now=noon))
        self.write(15, 3)
        audit.publish()
        self.assertTrue((self.folder() / f"{self.day(14)}.jsonl").is_file())
        audit.AUDIT = self.elsewhere  # the machine that set it is not this one
        self.assertIn("fix/unshipped", branches.embargo_trail("acme/app"))

    def test_verify_finds_a_day_removed_from_the_middle_of_the_daily_files(self):
        self.assertEqual(len(self.publish_three_days()), 3)
        audit.AUDIT = self.elsewhere
        (self.folder() / f"{self.day(15)}.jsonl").unlink()
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual((Path(res["first"]["path"]).name, res["first"]["line"], res["first"]["kind"]),
                         (f"{self.day(16)}.jsonl", 1, "unlinked"))

    def test_a_day_changed_here_after_it_was_published_is_reported_and_never_published_over(self):
        self.assertEqual(len(self.publish_three_days()), 3)
        published = self.folder() / f"{self.day(15)}.jsonl"
        before = published.read_bytes()
        lines = self.month().read_bytes().splitlines(keepends=True)
        lines[4] = lines[4].replace(b'"outcome":"ok"', b'"outcome":"failed"')  # the 15th's second record
        self.month().write_bytes(b"".join(lines))
        res = audit.verify()
        diverged = [p for p in res["problems"] if p["kind"] == "diverged"]
        self.assertEqual([p["line"] for p in diverged], [5])  # the 15th's second record, in the local file
        self.assertIn(published.name, diverged[0]["problem"])
        self.assertEqual(self.daily(audit.publish()), [])
        self.assertEqual(published.read_bytes(), before)
        refused = audit.query(action="audit.publish")
        self.assertEqual([(r["outcome"], r["severity"]) for r in refused], [("blocked", "critical")])

    def test_a_day_over_the_limit_is_held_and_named_while_the_days_around_it_publish(self):
        self.write(14, 2)
        self.write(15, 10)
        self.write(16, 2)
        sizes = self.sizes()
        held_day = self.day(15)
        with self.limit((sizes[held_day] + max(sizes[self.day(14)], sizes[self.day(16)])) // 2):
            changed = audit.publish()
            self.assertEqual(self.daily(changed),
                             [f"{self.day(14)}.jsonl", f"{held_day}.held.json", f"{self.day(16)}.jsonl"])
            self.assertFalse((self.folder() / f"{held_day}.jsonl").exists())
            stub = json.loads((self.folder() / f"{held_day}.held.json").read_text())
            self.assertEqual((stub["day"], stub["bytes"], stub["records"]), (held_day, sizes[held_day], 10))
            held = audit.held()
            self.assertEqual([(h["month"], h["day"], h["bytes"]) for h in held], [(self.ym, held_day, sizes[held_day])])
            self.assertIn(f"{held_day} is not published", held[0]["text"])
            self.assertIn("local trail", held[0]["fix"])
            self.assertIn("dev audit verify", held[0]["fix"])
            self.assertIn(f"dev audit stats --since {held_day}", held[0]["fix"])
            rows = audit.publish_checks()
            self.assertEqual([r[2] for r in rows], [False])
            self.assertIn(held_day, rows[0][3])
            warn = audit.query(action="audit.publish")
            self.assertEqual([(w["outcome"], w["severity"]) for w in warn], [("blocked", "warning")])
            self.assertIn(held_day, warn[0]["detail"])
            self.assertTrue(audit.verify()["ok"])  # here, the held day still matches what its record says
        audit.AUDIT = self.elsewhere
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])  # elsewhere, the chain is followed across the held day
        self.assertTrue(any(held_day in n["note"] for n in res["notes"]))

    def test_a_copy_published_as_one_file_stays_and_the_daily_files_continue_after_it(self):
        self.write(14, 3)
        self.write(15, 1)
        whole = audit.team_dir() / self.month().name
        whole.parent.mkdir(parents=True)
        whole.write_bytes(self.month().read_bytes())  # as an earlier devkit published it: the whole month so far
        before = whole.read_bytes()
        self.write(15, 2)
        self.write(16, 3)
        changed = audit.publish()
        self.assertEqual(self.daily(changed), [f"{self.day(15)}.jsonl", f"{self.day(16)}.jsonl"])
        self.assertEqual(whole.read_bytes(), before)  # never rewritten nor removed
        self.assertEqual(before + b"".join(p.read_bytes() for p in changed), self.month().read_bytes())
        self.assertEqual(len(self.folder().joinpath(f"{self.day(15)}.jsonl").read_text().splitlines()), 2)
        self.assertTrue(audit.verify()["ok"])
        audit.AUDIT = self.elsewhere
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 9)

    def test_dev_doctor_fails_until_the_audit_job_publishes_the_daily_files(self):
        self.write(14, 3)
        self.write(15, 3)
        with self.limit(self.month().stat().st_size - 1):
            rows = audit.publish_checks()
            self.assertEqual([r[2] for r in rows], [False])
            self.assertIn(f"{self.ym} is not published yet", rows[0][3])
            self.assertIn("dev jobs run audit", rows[0][4])
            audit.publish()
            rows = audit.publish_checks()
            self.assertEqual([r[2] for r in rows], [True])
            self.assertIn(f"{self.ym} published as one file per UTC day", rows[0][3])

    def test_prune_removes_months_of_daily_files_older_than_team_months(self):
        old = audit.months_back(time.time(), 14)  # past the 13 months the handoff copy keeps by default
        gone = audit.team_dir() / old
        gone.mkdir(parents=True)
        names = [f"{old}-03.jsonl", f"{old}-04.held.json", f"{old}-05.jsonl"]
        for n in names:
            (gone / n).write_text("{}\n")
        kept = self.folder() / f"{self.day(14)}.jsonl"
        kept.parent.mkdir(parents=True)
        kept.write_text("{}\n")
        out = audit.prune()
        self.assertEqual(sorted(p.name for p in out["team"]), sorted(names))
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())


class PublishCleaningTests(AuditCase):
    # The audit job copies this machine's closed days into the handoff repository, whose git history keeps them for
    # good. Lines an older devkit wrote can hold what every reader now cleans: a typed configuration key, a job's
    # name, a whole jobs.set detail, a value's digest, a secret the redactor learned since. Each goes out as a reader
    # sees it, and a line already clean goes out as it is stored.
    TOKEN = "".join(("q7rk2plm", "9zt4wv8b", "n3hc6xd5"))  # in pieces: a fixture, not a credential to scan
    GITHUB = "ghp_" + A36
    DIGEST = "ab" * 32

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(core, "CONFIG_FILE", self.root / "config.json")  # the key rule reads no real file
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ym = audit.months_back(time.time(), 1)  # last month: every day is closed, so every day publishes
        self.month = audit.AUDIT / f"{self.ym}.jsonl"
        self.noon = calendar.timegm((int(self.ym[:4]), int(self.ym[5:]), 14, 12, 0, 0, 0, 0, 0))
        self.folder = audit.team_dir() / self.ym

    def old_v1(self, i: int, **fields):
        audit._private_dir(audit.AUDIT)
        line = {"ts": self.noon + i, "user": "u", "host": "h", "agent": "", "outcome": "ok", **fields}
        with self.month.open("a") as fh:
            fh.write(json.dumps(line) + "\n")

    def old_v2(self, i: int, action: str, target: str, detail: str, **more):
        rec = audit.build("jobs.set", "probe", "{}", now=self.noon + i)
        rec.update(action=action, target=target, detail=detail, **more)
        audit._append(rec)  # sealed as an older devkit sealed it: its hash covers the token

    def write_old_day(self):
        tok, gh = self.TOKEN, self.GITHUB
        self.old_v1(0, action=f"job.{tok}", target="status", detail="")
        self.old_v1(1, action="dev.config.set", target=f"jobs.{tok}.every", detail=f"rotated {gh}")
        with self.month.open("a") as fh:  # a line cut short, which no reader can read
            fh.write('{"ts": %d, "action": "jobs.set", "target": "%s\n' % (self.noon + 2, tok))
        self.old_v2(3, "dev.config.set", f"jobs.{tok}.every",
                    json.dumps({"value_type": "int", "value_digest": self.DIGEST}))
        self.old_v2(4, "dev.config.set", f"codex.profiles.{tok}.model", "{}")
        self.old_v2(5, "jobs.set", tok, json.dumps({"cmd": ["status", tok], "about": tok, "every": 30}))
        self.old_v2(6, "jobs.install", f"sync,{tok}", f"ok:sync; loaded:{tok}")
        self.old_v2(7, f"job.{tok}", "status", "fine", why=f"rotated {gh}")
        for d in (15, 16):  # records written today: already clean
            audit._append(audit.build("dev.task", f"add {d}", "fine", now=self.noon + (d - 14) * 86400))
        text = self.month.read_text()
        for secret in (tok, gh, self.DIGEST):
            self.assertIn(secret, text)  # stored as the older devkit wrote it

    def day(self, d: int) -> Path:
        return self.folder / f"{self.ym}-{d:02d}.jsonl"

    def published_records(self) -> list:
        out = []
        for f in audit.publish():
            out += [json.loads(ln) for ln in f.read_bytes().decode("utf-8").splitlines() if ln.strip()]
        return out

    def test_an_old_line_holding_a_lone_surrogate_publishes_as_a_reader_reads_it(self):
        # an older devkit wrote an argument's bytes that were not UTF-8 (a Latin-1 "café") as the escape \udce9, which
        # reads back as a lone surrogate that strict UTF-8 cannot write: the daily job stopped there
        self.old_v1(0, action="dev.task", target="t", detail="", why="caf\udce9")
        audit._append(audit.build("dev.task", "next", "", now=self.noon + 86400))
        whys = [r.get("why") for r in self.published_records()]
        self.assertIn("caf\\udce9", whys)  # its escape, as a reader reads it
        self.assertTrue(audit.verify()["ok"])

    def test_every_reader_of_an_old_line_holding_a_lone_surrogate_gives_text_utf8_can_write(self):
        # dev audit show, export and --format json|csv printed it to a UTF-8 terminal or file and stopped there
        self.old_v1(0, action="dev.task", target="t", detail="", why="caf\udce9")
        recs = audit.query(days=400)
        self.assertEqual(recs[0].get("why"), "caf\\udce9")  # its escape, as a new record stores it
        outs = {"show": json.dumps(audit.show(recs[0]["id"]), ensure_ascii=False),
                "table": json.dumps(audit.latest(5, days=400)[1], ensure_ascii=False)}
        outs.update({fmt: audit.export(fmt, days=400) for fmt in audit.FORMATS})
        for name, text in outs.items():
            with self.subTest(name):
                self.assertIn("udce9", text.encode("utf-8").decode("utf-8"))

    def test_a_v2_line_holding_a_lone_surrogate_does_not_stop_publish(self):
        # devkit never writes one (build writes its escape), so only a hand edit puts it there: it goes out as a reader
        # reads it, and the local chain reports the edit (its hash was sealed without it)
        rec = audit.build("dev.task", "t", "", now=self.noon)
        rec["why"] = "x\ud800"
        audit._private_dir(audit.AUDIT)
        with self.month.open("a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        audit._append(audit.build("dev.task", "next", "", now=self.noon + 86400))
        out = [r for r in self.published_records() if r.get("id") == rec["id"]]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("why"), "x\\ud800")
        self.assertFalse(audit.verify()["ok"])

    def commit_handoff(self) -> str:
        # the handoff repository as dev sync commits it -> every version git keeps of every file
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-C", str(core.HANDOFF), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "audit"]):
            subprocess.run(git + args, check=True, env=env, capture_output=True, timeout=60)
        return subprocess.run(git + ["log", "-p", "--all", "--format=%H"], check=True, env=env, capture_output=True,
                              text=True, timeout=60).stdout

    def views(self, team: bool) -> list:
        # what a reader sees of each record, oldest first; `cleaned` says only whether the stored line differed
        return [{k: v for k, v in r.items() if k != "cleaned"} for r in audit.query(team=team)]

    def test_old_lines_reach_the_team_repository_only_as_a_reader_sees_them(self):
        self.write_old_day()
        changed = audit.publish()
        self.assertEqual(changed, [self.day(14), self.day(15), self.day(16)])
        history = self.commit_handoff()
        files = "".join(p.read_text() for p in sorted(core.HANDOFF.rglob("*.jsonl")))
        self.assertIn("jobs.<key of 24 characters>.every", files)  # the records went out, cleaned
        for secret in (self.TOKEN, self.GITHUB, self.DIGEST):
            self.assertNotIn(secret, files)
            self.assertNotIn(secret, history)
        local = self.month.read_bytes().splitlines(keepends=True)
        self.assertEqual([self.day(15).read_bytes(), self.day(16).read_bytes()], local[-2:])  # clean: as stored
        self.assertEqual(self.views(team=True), self.views(team=False))  # each record once, read the same
        self.assertEqual(audit.publish(), [])  # a published day is written once
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["unpublished"], 0)
        here = self.views(team=False)
        audit.AUDIT = self.root / "elsewhere"  # a machine that holds none of these records
        self.assertEqual(self.views(team=True), here)
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 7)
        notes = [n["note"] for n in res["notes"]]
        self.assertEqual(sum(n.startswith("cleaned before it was published") for n in notes), 5, notes)  # the old v2 records
        self.assertEqual(sum("line before it was cleaned" in n for n in notes), 1, notes)  # the first, after v1 lines

    def test_an_old_value_change_reaches_the_team_repository_only_as_its_type(self):
        # devkit 26.11.3 to 26.11.18 recorded a dev.config.set value whole: "<old JSON> → <new JSON>"
        pw = self.TOKEN
        change = f"null → {json.dumps(['deploy', '--password', pw])}"
        self.old_v1(0, action="dev.config.set", target="progress.max_parallel", detail=f'null → "{pw}"')
        self.old_v2(1, "dev.config.set", "progress.max_parallel", change)
        audit._append(audit.build("dev.task", "next day", "fine", now=self.noon + 86400))
        self.assertIn(pw, self.month.read_text())
        self.assertEqual(audit.publish(), [self.day(14), self.day(15)])
        history = self.commit_handoff()
        files = "".join(p.read_text() for p in sorted(core.HANDOFF.rglob("*.jsonl")))
        self.assertNotIn(pw, files)
        self.assertNotIn(pw, history)
        self.assertEqual([r["detail"] for r in audit.query(team=True, action="dev.config.set")],
                         ["a string of 24 characters", "a list of 3 items"])
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])

    def text_notes(self, res: dict) -> list:
        # verify's notes on published lines that read differently from their local lines -> [(file, line)]
        return [(Path(n["path"]).name, n["line"]) for n in res["notes"] if "reads differently" in n["note"]]

    def test_the_anchor_tells_a_changed_line_from_a_redaction_pattern_added_since(self):
        self.write_old_day()
        audit.publish()
        core.CFG["audit"]["redact"]["extra"] = [r"rotated", r"status"]  # cleans the published lines further today
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(self.text_notes(res), [])
        self.assertEqual(audit.publish(), [])
        self.assertEqual(audit.query(action="audit.publish"), [])  # nothing blocked
        published = self.day(14).read_text().splitlines(keepends=True)
        n = next(i for i, ln in enumerate(published) if '"jobs.install"' in ln)
        rec = json.loads(published[n])
        self.assertTrue(rec["cleaned"])
        edits = {"outcome": dict(rec, outcome="failed"), "severity": dict(rec, severity="info"),
                 "ts": dict(rec, ts=rec["ts"] + 1), "actor": dict(rec, actor={**rec["actor"], "user": "eve"}),
                 "a key added": dict(rec, why="approved"), "a key removed": {k: v for k, v in rec.items()
                                                                              if k != "detail"}}
        for name, edit in edits.items():
            with self.subTest(edit=name):  # a cleaned record changed in the handoff repository, where cleaning never
                self.day(14).write_text("".join(published[:n] + [json.dumps(edit) + "\n"] + published[n + 1:]))
                res = audit.verify()
                self.assertFalse(res["ok"])
                self.assertEqual((res["first"]["kind"], res["first"]["line"]), ("diverged", n + 1))
        # its text changed: anchored by id, prev and hash, and noted, since a pattern changed since reads the same way
        self.day(14).write_text("".join(published[:n] + [json.dumps(dict(rec, target="sync")) + "\n"]
                                        + published[n + 1:]))
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(self.text_notes(res), [(self.day(14).name, n + 1)])
        self.day(14).write_text("".join(published))
        clean = json.loads(self.day(15).read_text())
        self.day(15).write_text(json.dumps(dict(clean, detail="other", cleaned=True)) + "\n")  # marked to pass
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(self.text_notes(res), [(self.day(15).name, 1)])
        self.day(15).write_bytes(self.month.read_bytes().splitlines(keepends=True)[-2])
        self.assertTrue(audit.verify()["ok"])
        lines = self.month.read_text().splitlines(keepends=True)
        n = next(i for i, ln in enumerate(lines) if '"jobs.install"' in ln)
        lines[n] = json.dumps(dict(json.loads(lines[n]), detail="ok:sync", cleaned=True)) + "\n"
        self.month.write_text("".join(lines))  # a local record, changed and marked: the local chain still checks it
        res = audit.verify(team=False)
        self.assertEqual((res["first"]["kind"], res["first"]["line"]), ("edited", n + 1))

    def test_a_v1_number_or_boolean_a_reader_hides_is_not_published(self):
        pin, phone = 48213579, 15551234567
        self.old_v1(0, action="dev.task", target=phone, detail=True)  # a reader says both as text, one by length
        self.old_v1(1, action="watch.wait", target="before", detail="", pin=pin)  # a key a reader drops
        self.old_v1(2, action="watch.wait", target="after", detail="")  # clean: as stored
        audit._append(audit.build("dev.task", "next day", "fine", now=self.noon + 86400))
        self.assertEqual(audit.publish(), [self.day(14), self.day(15)])
        published = self.day(14).read_bytes().splitlines(keepends=True)
        for n in (0, 1):
            self.assertTrue(json.loads(published[n])["cleaned"])
        self.assertNotIn(str(pin), published[1].decode())
        self.assertNotIn(str(phone), published[0].decode())
        self.assertEqual(json.loads(published[0])["detail"], "True")
        self.assertEqual(published[2], self.month.read_bytes().splitlines(keepends=True)[2])
        self.assertEqual(self.views(team=True), self.views(team=False))
        self.assertTrue(audit.verify()["ok"])

    def test_a_line_with_a_duplicate_key_is_published_as_its_record(self):
        # json.loads keeps a duplicate key's last value, so the stored bytes hold more than any reader sees
        gh = self.GITHUB
        audit._append(audit.build("watch.wait", "before", "", now=self.noon))
        self.old_v1(1, action="watch.wait", target="after", detail="")
        self.month.write_text("".join('{"target": "' + gh + '", ' + ln[1:]
                                      for ln in self.month.read_text().splitlines(keepends=True)))
        self.assertTrue(audit.verify(team=False)["ok"])  # the record parsed and hashed is the one sealed
        audit._append(audit.build("dev.task", "next day", "fine", now=self.noon + 86400))
        audit.publish()
        self.assertNotIn(gh, self.day(14).read_text())
        self.assertEqual(self.views(team=True), self.views(team=False))
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])

    def test_a_stand_in_matches_only_an_unreadable_local_line_and_holds_nothing_else(self):
        self.write_old_day()
        audit.publish()
        last, d14 = self.day(16).read_bytes(), self.day(14).read_bytes()
        empty = json.dumps({"v": 1, "id": audit._v1_id({}, b""), "cleaned": True, "unreadable": True})
        for name, tail in (("after the last line", empty), ("with its line end", empty + "\n")):
            with self.subTest(extra=name):  # a stand-in for a line past the local file's end
                self.day(16).write_bytes(last + tail.encode())
                res = audit.verify()
                self.assertFalse(res["ok"])
                self.assertEqual((res["first"]["kind"], res["first"]["line"]), ("diverged", 11))  # the local file has 10 lines
        self.day(16).write_bytes(last)
        lines = d14.splitlines(keepends=True)
        stand_in = json.loads(lines[2])
        self.assertEqual(set(stand_in), {"v", "id", "cleaned", "unreadable"})  # the line cut short
        self.day(14).write_bytes(b"".join(lines[:2] + [json.dumps(dict(stand_in, detail="it was fine")).encode()
                                                       + b"\n"] + lines[3:]))
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual((res["first"]["kind"], res["first"]["line"]), ("diverged", 3))
        self.day(14).write_bytes(d14)
        self.assertTrue(audit.verify()["ok"])

    def test_a_redaction_pattern_removed_after_publish_blocks_nothing(self):
        # this Mac's pattern hid text in old lines when they were published; removed since, today's reading of the
        # local line shows that text, which the published line does not: noted, never a divergence
        self.write_old_day()
        core.CFG["audit"]["redact"]["extra"] = [r"fine"]
        self.assertEqual(audit.publish(), [self.day(14), self.day(15), self.day(16)])
        core.CFG["audit"]["redact"]["extra"] = []
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(sorted(self.text_notes(res)), [(self.day(14).name, 8), (self.day(15).name, 1),
                                                        (self.day(16).name, 1)])
        audit._append(audit.build("dev.task", "a later day", "", now=self.noon + 3 * 86400))
        self.assertEqual(audit.publish(), [self.day(17)])  # the next day goes out
        self.assertEqual(audit.query(action="audit.publish"), [])  # nothing blocked

    def test_a_team_policy_broken_after_publish_blocks_nothing_and_names_the_file(self):
        self.write_old_day()
        f = audit.policy_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"redact": {"extra": ["fine"]}}')
        self.assertEqual(audit.publish(), [self.day(14), self.day(15), self.day(16)])
        f.write_text('{"redact": {"extra": ["("]}}')  # a teammate's commit breaks it
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(self.text_notes(res), [])
        unchecked = [n["note"] for n in res["notes"] if "not compared" in n["note"]]
        self.assertEqual(len(unchecked), 1, res["notes"])
        self.assertIn(core.tilde(f), unchecked[0])
        f.write_text('{"redact": {"extra": ["fine"]}}')  # repaired
        audit._append(audit.build("dev.task", "a later day", "", now=self.noon + 3 * 86400))
        self.assertEqual(audit.publish(), [self.day(17)])
        self.assertEqual(audit.query(action="audit.publish"), [])
        self.assertTrue(audit.verify()["ok"])

    def test_nothing_is_published_while_the_redaction_patterns_cannot_load(self):
        self.write_old_day()
        f = audit.policy_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"redact": {"extra": ["("]}}')  # a pattern that does not compile
        self.assertEqual(audit.publish(), [])
        self.assertFalse(self.folder.exists())
        refused = audit.query(action="audit.publish")
        self.assertEqual([(r["outcome"], r["severity"]) for r in refused], [("blocked", "critical")])
        f.unlink()
        self.assertEqual(audit.publish(), [self.day(14), self.day(15), self.day(16)])


class DeepNestingTests(AuditCase):
    # Any teammate can write a line into the handoff repository's audit copies. A line nested 100,000 levels deep
    # must read as one no reader can read, as a line cut short does, instead of exhausting the stack: `dev audit
    # --team` lists the other records and `dev audit verify` names the line. Python 3.15 parses 100,000 levels and
    # then fails in what reads the value (repr, dumps); a million is past the parser's own limit on every Python.
    DEPTHS = (100_000, 1_000_000)

    @staticmethod
    def shapes(d: int, now: float = None) -> dict:
        now, deep_obj, deep_list = now or time.time(), '{"a":' * d + "1" + "}" * d, "[" * d + "]" * d
        v2 = json.dumps({"v": 2, "id": "01JDEEPNESTEDRECORD0000000", "ts": now, "action": "watch.wait",
                         "hash": "0" * 64, "actor": {"user": "mate", "host": "mini", "agent": "", "session": ""}})
        v1 = json.dumps({"ts": now, "action": "watch.wait", "user": "mate", "host": "mini", "target": "deep"})
        return {"a bare array": deep_list,
                "a v2 record's evidence": v2[:-1] + ', "evidence": ' + deep_obj + "}",
                "a v1 record's why": v1[:-1] + ', "why": ' + deep_list + "}"}

    @staticmethod
    def v1(target: str, **more) -> str:
        return json.dumps({"ts": time.time(), "action": "watch.wait", "user": "mate", "host": "mini", "target": target,
                           "detail": "", "outcome": "ok", **more})

    def team_copy(self, *lines) -> Path:
        f = core.HANDOFF / "audit" / "mate@mini" / f"{time.strftime('%Y-%m')}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(ln + "\n" for ln in lines))
        return f

    def team_view(self) -> tuple:
        # `dev audit --team` -> (exit code, output)
        import argparse
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(["--team"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = audit.cmd_audit(a)
        return rc, out.getvalue()

    def test_a_deeply_nested_line_in_a_team_copy_reads_as_unreadable_and_stops_no_reader(self):
        for d in self.DEPTHS:
            for shape, deep in self.shapes(d).items():
                with self.subTest(depth=d, shape=shape):
                    f = self.team_copy(self.v1("before"), deep, self.v1("after"))
                    rc, text = self.team_view()
                    self.assertEqual(rc, 0)
                    self.assertIn("· team · 2 record(s)", text)
                    self.assertEqual(re.findall(r"watch\.wait\s+(before|after|deep)\b", text), ["before", "after"])
                    res = audit.verify()
                    self.assertTrue(res["ok"], res["first"])  # v1 lines: no chain for the line to break
                    noted = [n for n in res["notes"] if n["path"] == str(f)]
                    self.assertEqual([(n["line"], n["id"], n["kind"]) for n in noted],
                                     [(2, audit._v1_id({}, deep.encode()), "note")])
                    self.assertIn("cannot be read", noted[0]["note"])

    def test_a_deeply_nested_line_in_a_chain_breaks_only_the_link_it_was_sealed_into(self):
        # a teammate's v2 record replaced with a deep line: the record after it no longer links, and verify says so
        audit._append(audit.build("dev.task", "one", now=time.time()))
        audit._append(audit.build("dev.task", "two", now=time.time()))
        local = sorted(audit.AUDIT.glob("*.jsonl"))[0].read_text().splitlines()
        f = self.team_copy(local[0], self.shapes(100_000)["a v2 record's evidence"], local[1])
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual((res["first"]["path"], res["first"]["line"], res["first"]["kind"]), (str(f), 3, "unlinked"))
        self.assertEqual([n["line"] for n in res["notes"] if n["path"] == str(f)], [2])

    def test_a_deeply_nested_detail_in_an_old_record_leaves_the_record_readable(self):
        # an older devkit's jobs.set detail is JSON a reader parses again: past any depth, the detail stays text
        for d in self.DEPTHS:
            with self.subTest(depth=d):
                self.team_copy(self.v1("before"), json.dumps({"ts": time.time(), "action": "jobs.set", "user": "mate",
                                                              "host": "mini", "target": "nightly",
                                                              "detail": '{"every":' * d + "1" + "}" * d}))
                rc, text = self.team_view()
                self.assertEqual(rc, 0)
                self.assertIn("· team · 2 record(s)", text)
                self.assertEqual([r["target"] for r in audit.query(team=True)], ["before", "nightly"])

    def test_a_deeply_nested_local_line_is_published_as_a_stand_in_and_the_anchor_holds(self):
        ym = audit.months_back(time.time(), 1)  # last month: every day is closed
        noon = calendar.timegm((int(ym[:4]), int(ym[5:]), 14, 12, 0, 0, 0, 0, 0))
        audit._append(audit.build("watch.wait", "before", now=noon))
        month = audit.AUDIT / f"{ym}.jsonl"
        deep = self.shapes(100_000, now=noon + 1)["a v1 record's why"]
        with month.open("a") as fh:
            fh.write(deep + "\n")
        audit._append(audit.build("watch.wait", "after", now=noon + 2))
        self.assertEqual(audit.publish(), [audit.team_dir() / ym / f"{ym}-14.jsonl"])
        published = (audit.team_dir() / ym / f"{ym}-14.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(published[1]), {"v": 1, "id": audit._v1_id({}, deep.encode()), "cleaned": True,
                                                    "unreadable": True})
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["unpublished"], 0)
        self.assertEqual([r["target"] for r in audit.query(team=True, since=ym + "-01")], ["before", "after"])

    def test_a_deeply_nested_held_day_record_reads_as_one_that_cannot_be_read(self):
        ym = time.strftime("%Y-%m")
        held = core.HANDOFF / "audit" / "mate@mini" / ym / f"{ym}-01.held.json"
        held.parent.mkdir(parents=True)
        held.write_text('{"bytes": 1, "sha256": "x", "day": ' + "[" * 1_000_000 + "]" * 1_000_000 + "}")
        res = audit.verify()
        self.assertEqual((res["first"]["path"], res["first"]["kind"]), (str(held), "unreadable"))

    def test_a_deeply_nested_team_policy_fails_closed_as_one_that_is_not_json(self):
        f = audit.policy_file()
        f.parent.mkdir(parents=True)
        for d in self.DEPTHS:
            body = '{"redact": {"extra": ' + "[" * d + "]" * d + "}}"
            with self.subTest(depth=d):
                f.write_text(body)
                cfg = audit.settings()
                self.assertIn("is not valid JSON", audit.redaction_unavailable(cfg))
                self.assertEqual(audit.publishable_policy(f), "is not valid JSON")
                self.assertEqual(audit.publishable_text("audit.json", body.encode()), "is not valid JSON")


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
