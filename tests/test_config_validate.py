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


class ConfigValueTests(unittest.TestCase):
    # A wrong value and a map key in a path were said as the person typed them: a token pasted as a value, or as the
    # name of a job, a Codex profile or an MCP server's variable, reached every reader's error. Each reader now names
    # a value by its path, type and length, and a map key only when it is a short word.
    DOC = {"work": {"mode": GITHUB}, "stacks": [GITHUB], "sync": GITHUB, "docker": {"cpu": [GITHUB, GITHUB]},
           "codex": {"profiles": {GITHUB: {"model": 3, "extra": 1}}},
           "jobs": {TEAM_WORD: {"every": 5}, "backup": {"every": 5}},
           "ext": {"add": [{"kind": "mcp", "license": "MIT", "why": "w", "name": "srv",
                            "url": "https://example.invalid/mcp", "env": {GITHUB: 5}}]}}
    SAID = ("work.mode must be one of auto, guided (got a string of 40 characters)",
            "stacks[0] must be one of java, python, php, node (got a string of 40 characters)",
            "sync must be an object (got a string of 40 characters)",
            "docker.cpu must be a whole number greater than zero (got a list of 2 items)",
            "codex.profiles.<key of 40 characters>.model must be a string (got a number)",
            "unknown key codex.profiles.<key of 40 characters>.extra",
            "jobs.<key of 21 characters>.cmd is required for a custom job",
            "jobs.backup.cmd is required for a custom job",  # a short word is still named
            "ext.add[0].env.<key of 40 characters> must be a string (got a number)")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text(json.dumps(self.DOC))
        for p in (mock.patch.object(core, "CONFIG_FILE", self.path),
                  mock.patch.object(core, "STATE", Path(folder.name) / "state"),
                  mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: Path(folder.name) / "no-policy.json"),
                  mock.patch.object(profiles, "resolve", side_effect=ValueError("stopped by the test"))):
            p.start()
            self.addCleanup(p.stop)

    def said(self, text: str, lines=SAID):
        for secret in (GITHUB, TEAM_WORD):
            self.assertNotIn(secret, text)
        for line in lines:
            self.assertIn(line, text)

    def run_quietly(self, call):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = call()
        return rc, out.getvalue() + err.getvalue()

    def test_every_reader_of_the_saved_configuration_says_no_pasted_value_or_map_key(self):
        update = argparse.Namespace(check=False, quiet=False, record=False, yes=False)
        for name, call, code in (("every dev command", core.load_config, None),
                                 ("dev config show", lambda: cli.cmd_config(argparse.Namespace(config_cmd="show")), 1),
                                 ("dev config validate", lambda: cli.cmd_config(
                                     argparse.Namespace(config_cmd="validate")), 1),
                                 ("dev update", lambda: cli.cmd_update(update), 2),
                                 ("dev install", setup.run_install, 2)):
            with self.subTest(reader=name):
                rc, text = self.run_quietly(call)
                if code is not None:
                    self.assertEqual(rc, code)
                # dev install names only the errors that stop it: not the key devkit does not read
                self.said(text, self.SAID if name != "dev install" else self.SAID[:5] + self.SAID[6:])
        stop, _ = setup.config_diagnosis(core.config_snapshot()[1])  # dev doctor
        self.said(stop)

    def test_dev_config_set_says_no_pasted_value_or_map_key(self):
        self.path.write_text("{}")
        for key, value, line in (
                ("work.mode", GITHUB, "work.mode must be one of auto, guided (got a string of 40 characters)"),
                ("stacks", json.dumps([GITHUB]), "stacks[0] must be one of java, python, php, node "
                                                 "(got a string of 40 characters)"),
                (f"jobs.{TEAM_WORD}.every", "5", "jobs.<key of 21 characters>.cmd is required for a custom job"),
                (f"jobs.{TEAM_WORD}.cmd.x", "1", "jobs.<key of 21 characters>.cmd must be an object to set a child"),
                (f"codex.profiles.{GITHUB}.model", "3",
                 "codex.profiles.<key of 40 characters>.model must be a string (got a number)")):
            with self.subTest(key=key[:10]):
                with self.assertRaises(core.ConfigError) as caught:
                    core.config_set(key, value)
                self.said(str(caught.exception), (line,))
        self.assertEqual(self.path.read_text(), "{}")

    def test_a_writer_refusing_a_candidate_says_no_pasted_value_or_map_key(self):
        self.path.write_text("{}")
        with self.assertRaises(core.ConfigError) as caught:
            core.config_update(lambda doc: dict(self.DOC))
        self.said(str(caught.exception))

    def test_a_quoted_value_from_a_section_check_is_said_by_type_and_length(self):
        # progress_config and audit_config quote up to 40 characters of a wrong value: the seam says its type instead
        raw = {"progress": {"max_parallel": GITHUB, "notify": [1, 2, 3]}}
        said = core.say_config_errors(core.progress_config(raw["progress"])[1], raw)
        self.assertEqual(said, ["progress.max_parallel must be a whole number from 1 to 10 "
                                "(got a string of 40 characters)",
                                "progress.notify must be true or false (got a list of 3 items)"])
        self.assertEqual(core.say_config_errors(["progress.nudge_minutes must be a number (got \"secret\")"], {}),
                         ["progress.nudge_minutes must be a number (got a value it does not quote)"])


if __name__ == "__main__":
    unittest.main()
