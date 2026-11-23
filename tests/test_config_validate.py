# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev config validate` names a key devkit does not read only when it is a short word no secret rule or pattern finds
# anything in, as `dev audit config` does: a token pasted as a key is said by its length and where it is. Its output
# lands in terminals, session logs and screenshots shared with the team.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_config_validate
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, cli, core, profiles, setup  # noqa: E402

GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # a shape the built-in rules know
TEAM_WORD = "acmetok7f3k9x2m4q8zr1"  # a digit in it: not a short word
TEAM_ONLY = "zuluquebec"  # a short word only this Mac's pattern knows


class ConfigValidateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        for p in (mock.patch.object(core, "CONFIG_FILE", self.path),
                  mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: Path(folder.name) / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def validate(self, doc: dict) -> tuple:
        self.path.write_text(json.dumps(doc))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = cli._config_validate()
        return rc, out.getvalue()

    def test_an_unknown_key_is_named_only_when_it_is_a_short_word(self):
        rc, out = self.validate({"progress": {GITHUB: 3, "max_paralel": 3}, TEAM_WORD: 1, "work.mode": "auto",
                                 "audit": {"redact": {"extra": [TEAM_ONLY]}}, "jobs": {GITHUB: {"every": "1h"}},
                                 "sync": {TEAM_ONLY: True}})
        self.assertEqual(rc, 1)
        for secret in (GITHUB, TEAM_WORD, TEAM_ONLY):
            self.assertNotIn(secret, out)
        for said in (f"unknown key of {len(GITHUB)} characters in progress: devkit does not read it",
                     f"unknown key of {len(TEAM_WORD)} characters: devkit does not read it",
                     f"unknown key of {len(TEAM_ONLY)} characters in sync",
                     f"a map key of {len(GITHUB)} characters in jobs is invalid",
                     "unknown key progress.max_paralel: devkit does not read it",
                     "unknown key work.mode: devkit does not read it"):  # a short word with a dot, at the top
            self.assertIn(said, out)


class ConfigReaderTests(unittest.TestCase):
    # Every other reader said a key devkit does not read word for word: the error every dev command prints while the
    # configuration is invalid (core.load_config), dev config set, dev config show, dev update, dev install and
    # dev doctor. Each now names a key only when it is a short word, and otherwise its length and where it is.
    DOC = {"progress": {GITHUB: 3}, TEAM_WORD: 1, "sync": {"pussh": True}}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text(json.dumps(self.DOC))
        for p in (mock.patch.object(core, "CONFIG_FILE", self.path),
                  mock.patch.object(core, "STATE", Path(folder.name) / "state"),
                  mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: Path(folder.name) / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def said(self, text: str, length_of=(GITHUB, TEAM_WORD)):
        for secret in (GITHUB, TEAM_WORD):
            self.assertNotIn(secret, text)
        for key in length_of:
            self.assertIn(f"unknown key of {len(key)} characters", text)

    def run_quietly(self, call):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = call()
        return rc, out.getvalue() + err.getvalue()

    def test_the_error_every_dev_command_prints_names_no_pasted_key(self):
        rc, text = self.run_quietly(core.load_config)
        self.said(text)
        self.assertIn("unknown key of 40 characters in progress", text)
        self.assertIn("unknown key sync.pussh: devkit does not read it", text)  # a short word is still named

    def test_dev_config_set_names_no_pasted_key(self):
        for key in (f"progress.{GITHUB}", TEAM_WORD, f"jobs.{GITHUB}.every"):
            with self.subTest(key=key[:12]):
                with self.assertRaises(core.ConfigError) as caught:
                    core.config_set(key, "3")
                self.assertNotIn(GITHUB, str(caught.exception))
                self.assertNotIn(TEAM_WORD, str(caught.exception))
                self.assertRegex(str(caught.exception), r"(unknown|map) key of \d+ characters")
        with self.assertRaises(core.ConfigError) as caught:  # a short word is still named
            core.config_set("progress.max_paralel", "3")
        self.assertIn("unknown key progress.max_paralel", str(caught.exception))

    def test_a_writer_refusing_a_candidate_names_no_pasted_key(self):
        with self.assertRaises(core.ConfigError) as caught:
            core.config_update(lambda doc: {**doc, "sync": {GITHUB: True}})
        self.said(str(caught.exception), length_of=(GITHUB,))

    def test_dev_config_show_names_no_pasted_key(self):
        rc, text = self.run_quietly(lambda: cli.cmd_config(argparse.Namespace(config_cmd="show")))
        self.assertEqual(rc, 1)
        self.said(text)

    def test_dev_update_and_dev_install_name_no_pasted_key(self):
        stop = mock.patch.object(profiles, "resolve", side_effect=ValueError("stopped by the test"))
        with stop:
            rc, text = self.run_quietly(lambda: cli.cmd_update(argparse.Namespace(check=False, quiet=False,
                                                                                    record=False, yes=False)))
            self.assertEqual(rc, 2)
            self.said(text)
            rc, text = self.run_quietly(setup.run_install)
            self.assertEqual(rc, 2)
            self.said(text)
        # a value devkit reads is wrong too: the refusal says every error, and still no pasted key
        self.path.write_text(json.dumps({**self.DOC, "jobs": {GITHUB: {"every": "1h"}}, "gc": {"auto": "yes"}}))
        for call in (lambda: cli.cmd_update(argparse.Namespace(check=False, quiet=False, record=False, yes=False)),
                     setup.run_install):
            rc, text = self.run_quietly(call)
            self.assertEqual(rc, 2)
            self.said(text, length_of=())  # dev install names only the errors that stop it
            self.assertIn(f"a map key of {len(GITHUB)} characters in jobs is invalid", text)

    def test_dev_doctor_names_no_pasted_key(self):
        _, errors = core.config_snapshot()
        stop, row = setup.config_diagnosis(errors)
        self.assertIsNone(stop)
        self.said(row[3])
        self.path.write_text(json.dumps({**self.DOC, "gc": {"auto": "yes"}}))
        _, errors = core.config_snapshot()
        stop, row = setup.config_diagnosis(errors)
        self.said(stop)


if __name__ == "__main__":
    unittest.main()
