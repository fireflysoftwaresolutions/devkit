# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev impeccable <verb> [args…]` (lib/devkit/impeccable.py) runs the impeccable engine devkit installed, doing what the
# skill's launcher does without a shell and without a typed path: the engine's path comes from devkit's own skills
# folder, its SHA-256 is checked against the install record (STATE/skill-engines.json) before every run, the words
# after `dev impeccable` reach it as an argument list, and its environment carries devkit's IMPECCABLE_* settings and no
# other. The skill, a made-up engine (a Python script that writes down what it got) and the install record are put in
# the run's temporary home by the real install step, with only the download faked. The tests run bin/dev, as a shell
# does, in a project folder of the temporary home.
# Run: python3 -m unittest tests.test_impeccable_wrapper -v
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, impeccable, setup  # noqa: E402

# The made-up engine: writes its arguments, folder and IMPECCABLE_* variables to $FAKE_ENGINE_OUT, then exits with
# $FAKE_ENGINE_RC. Run by the interpreter running the tests, isolated (-I).
ENGINE = (f"#!{sys.executable} -I\n"
          "import json, os, sys\n"
          "with open(os.environ['FAKE_ENGINE_OUT'], 'w') as fh:\n"
          "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
          "               'env': {k: v for k, v in os.environ.items() if k.startswith('IMPECCABLE_')}}, fh)\n"
          "sys.exit(int(os.environ.get('FAKE_ENGINE_RC', '0')))\n").encode()
FIX = "dev install --only skills"


class WrapperCase(unittest.TestCase):
    def setUp(self):
        self.plat = setup.engine_platform()
        if not self.plat:
            self.skipTest("impeccable has no engine for this platform")
        self.skill = setup.SKILLS_SHARE / "impeccable"
        self.addCleanup(self.cleanup)
        self.cleanup()
        scripts = self.skill / "scripts"
        scripts.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: impeccable\n---\n")
        (scripts / "impeccable").write_text("#!/bin/sh\nexit 99\n")  # the launcher: dev impeccable never runs it
        (scripts / "VERSION").write_text("0.1.18\n")
        (scripts / "engine-checksums.txt").write_text(
            f"0.1.18 impeccable-{self.plat} {hashlib.sha256(ENGINE).hexdigest()}\n")
        with mock.patch.object(setup, "_download_engine", lambda url, dest: Path(dest).write_bytes(ENGINE)), \
                mock.patch.object(setup, "_audit", lambda *a, **k: None), contextlib.redirect_stdout(io.StringIO()):
            res = setup.install_impeccable_engine(self.skill, "pbakaus/impeccable")
        self.assertEqual(res["outcome"], "installed")
        self.engine = scripts / "bin" / self.plat / "impeccable"
        owner = core.DEV / "acme-impeccable"
        owner.mkdir(parents=True, exist_ok=True)
        self.project = Path(tempfile.mkdtemp(dir=owner))  # the caller's folder: a project checkout in the workspace
        self.git_init(self.project)
        self.box = Path(tempfile.mkdtemp(dir=core.HOME))  # where the engine writes down what it got
        for d in (owner, self.box):
            self.addCleanup(shutil.rmtree, d, True)
        self.out = self.box / "engine.json"

    @staticmethod
    def git_init(folder):
        subprocess.run(["git", "init", "-q", str(folder)], check=True, capture_output=True, timeout=30)

    def cleanup(self):
        for d in (self.skill, setup.ENGINES_SHARE / "impeccable"):
            shutil.rmtree(d, ignore_errors=True)
        (core.STATE / setup.ENGINES_FILE).unlink(missing_ok=True)

    def dev(self, *args, env=None):
        # `dev impeccable <args>` run by bin/dev from the project folder, as a shell runs it, with the run's
        # temporary home -> (exit code, stderr, what the engine got or None)
        p = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "bin" / "dev"), "impeccable", *args],
                           cwd=self.project, env={**os.environ, "FAKE_ENGINE_OUT": str(self.out), **(env or {})},
                           capture_output=True, text=True, timeout=60)
        got = json.loads(self.out.read_text()) if self.out.exists() else None
        return p.returncode, p.stderr, got

    def run_as(self, args, cwd=None):
        # impeccable.run with the words the engine would get, as an agent's session calls it (Claude Code's marker set),
        # in the project or in `cwd` -> (exit code, stderr, the engine's argv or None)
        if self.out.exists():
            self.out.unlink()
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_OUT": str(self.out), "CLAUDECODE": "1"}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = impeccable.run(args, cwd=str(cwd or self.project))
        got = json.loads(self.out.read_text()) if self.out.exists() else {}
        return rc, err.getvalue(), got.get("argv")


class RunTests(WrapperCase):
    def test_each_word_reaches_the_engine_as_one_argument_without_a_shell(self):
        # a reply's words take any text, so an agent's call carries this one too
        reason = "src/$(touch pwned); touch owned `touch ticked` & echo *"
        rc, err, got = self.dev("live-poll", "--reply", "e1", "error", reason)
        self.assertEqual(rc, 0, err)
        self.assertEqual(got["argv"], ["live-poll", "--reply", "e1", "error", reason])
        self.assertEqual(sorted(os.listdir(self.project)), [".git"])  # no shell ran the substitutions

    def test_the_engine_runs_in_the_callers_folder(self):
        rc, err, got = self.dev("context")
        self.assertEqual(rc, 0, err)
        self.assertEqual(os.path.realpath(got["cwd"]), os.path.realpath(self.project))

    def test_the_engine_gets_devkits_impeccable_settings_and_no_other(self):
        rc, err, got = self.dev("detect", "src", env={
            "IMPECCABLE_BIN": "/tmp/evil", "IMPECCABLE_SKILL_DIR": "/tmp/other", "IMPECCABLE_HOME": "/tmp/h",
            "IMPECCABLE_DOWNLOAD_BASE": "https://example.com", "IMPECCABLE_SYNTAX_PLUGINS": "/tmp/p.js",
            "IMPECCABLE_NO_TELEMETRY": "0", "IMPECCABLE_SELF": "/tmp/evil"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(got["env"], {"IMPECCABLE_SKILL_DIR": os.path.realpath(self.skill),
                                      "IMPECCABLE_SELF": "dev impeccable", "IMPECCABLE_NO_UPDATE_CHECK": "1",
                                      "IMPECCABLE_NO_TELEMETRY": "1"})

    def test_the_engines_exit_code_is_dev_impeccables(self):
        rc, err, got = self.dev("detect", "src", env={"FAKE_ENGINE_RC": "3"})
        self.assertEqual((rc, got and got["argv"]), (3, ["detect", "src"]), err)


class RefusalTests(WrapperCase):
    def test_no_install_record_runs_nothing(self):
        record = core.STATE / setup.ENGINES_FILE
        for text in (None, "{}\n", '{"impeccable": {"engine": "x"}}\n'):
            with self.subTest(record=text):
                if text is None:
                    record.unlink()
                else:
                    record.write_text(text)
                rc, err, got = self.dev("context")
                self.assertNotEqual(rc, 0)
                self.assertIsNone(got)
                self.assertIn(FIX, err)

    def test_an_engine_whose_digest_does_not_match_the_record_runs_nothing(self):
        with open(self.engine, "ab") as fh:  # still a valid script: only the digest tells it apart
            fh.write(b"# changed\n")
        rc, err, got = self.dev("context")
        self.assertNotEqual(rc, 0)
        self.assertIsNone(got)
        self.assertIn("SHA-256", err)
        self.assertIn(FIX, err)

    def test_an_engine_outside_devkits_skills_folder_runs_nothing(self):
        # The same bytes outside the share: the digest matches, only the path tells it apart. First the platform
        # folder is a link out of the share, then the record names the outside copy.
        outside = Path(tempfile.mkdtemp(dir=core.HOME))
        self.addCleanup(shutil.rmtree, outside, True)
        shutil.copy2(self.engine, outside / "impeccable")
        record = core.STATE / setup.ENGINES_FILE
        rec = json.loads(record.read_text())
        plat_dir = self.engine.parent
        shutil.move(str(plat_dir), str(outside / "kept"))
        plat_dir.symlink_to(outside)
        rc, err, got = self.dev("context")
        self.assertEqual((rc != 0, got), (True, None), err)
        self.assertIn(FIX, err)
        plat_dir.unlink()
        shutil.move(str(outside / "kept"), str(plat_dir))
        self.assertEqual(self.dev("context")[0], 0)
        rec["impeccable"]["engine"] = str(outside / "impeccable")
        record.write_text(json.dumps(rec))
        self.out.unlink()
        rc, err, got = self.dev("context")
        self.assertEqual((rc != 0, got), (True, None), err)
        self.assertIn(FIX, err)


class AnyCallerTests(WrapperCase):
    # The verb and its words are the caller's: once the engine is verified, an agent's call runs any verb the engine
    # has (the ones that write the agents' settings and hooks included), in any folder, with any path.
    def test_an_agents_call_runs_any_verb(self):
        for args in (["hooks", "on"], ["doctor", "--fix"], ["skills", "update"], ["polish"], ["concept-seed"],
                     ["build-phase", "advance", "--force", "--reason", "x"], []):
            with self.subTest(args=args):
                rc, err, argv = self.run_as(args)
                self.assertEqual((rc, argv), (0, args), err)
        # through bin/dev with Claude Code's marker: its parser drops one first `--`, and the rest reaches the engine
        rc, err, got = self.dev("--", "--", "hooks", "on", env={"CLAUDECODE": "1"})
        self.assertEqual((rc, got and got["argv"]), (0, ["--", "hooks", "on"]), err)

    def test_an_agents_call_runs_outside_a_project_checkout_and_with_paths_outside_it(self):
        claude = core.HOME / ".claude"
        claude.mkdir(exist_ok=True)
        rc, err, argv = self.run_as(["live", "--target", "settings.json"], cwd=claude)
        self.assertEqual((rc, argv), (0, ["live", "--target", "settings.json"]), err)
        away = os.path.relpath(self.box / "x.html", self.project)
        rc, err, argv = self.run_as(["surface-brief", "read", away])
        self.assertEqual((rc, argv), (0, ["surface-brief", "read", away]), err)


if __name__ == "__main__":
    unittest.main()
