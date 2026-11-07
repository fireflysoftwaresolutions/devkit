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
# impeccable's verbs that write the agents' settings, hooks or skill folders, in the forms dev passes on: the legacy
# `skills` namespace, once or more, a first `--`, and doctor's --fix
HARNESS = (["hooks", "on"], ["install", "--user"], ["skills", "update"], ["pin", "audit"], ["doctor", "--fix"],
           ["--", "hooks", "off"], ["skills", "skills", "hooks", "on"])
# the words the engine gets for the verbs agents run (the allowlist), the skill's helpers in the forms it uses included
AGENT_VERBS = (["context"], ["detect", "src"], ["doctor", "--json"], ["doctor", "--target", "src"], ["live"],
               ["generate"], ["document"], ["extract"], ["init"], ["live", "--target", "src/app"],
               ["detect", "http://localhost:5173/"], ["live-poll", "--reply", "e1", "done", "--file", "src/index.html"],
               ["live-wrap", "--id", "e1", "--count", "3", "--tag", "h1", "--file", "src/index.html"],
               ["surface-brief", "read", "src/index.html"], ["critique-storage", "latest", "src/index.html", "--json"],
               ["comp-spec", "--comp", "comp.png", "--grid"], ["build-phase", "advance"])
# words the engine may run but no agent may: hidden verbs of the 0.1.18 engine, its other visible ones, a design
# command that is not an engine verb, a verb a newer engine could add, no verb, the allowlist's verbs spelled
# otherwise (another case, after `skills` or `--`, doctor with another word), helpers that reach the internet, and
# helper forms that skip the skill's gates or write outside the project
DEVELOPER_ONLY = (["hook-admin"], ["hook"], ["hook-before-edit"], ["capture-server"], ["ignore", "x"],
                  ["ignores", "add", "x"], ["telemetry", "on"], ["check"], ["help"], ["polish"],
                  ["a-verb-a-newer-engine-adds"], [], ["Context"], ["skills", "context"], ["--", "context"],
                  ["doctor", "--FIX"], ["doctor", "--target", "src", "--fix"], ["doctor", "-f"], ["concept-seed"],
                  ["generate-image", "--prompt-file", "p.txt"], ["build-phase", "advance", "--force", "--reason", "x"],
                  ["build-phase", "start", "--comp", "comp.png"], ["live-complete", "--id", "e1", "--force"],
                  ["live", "--target", "../app"], ["live-wrap", "--id", "e1", "--file", "../x.html"],
                  ["comp-spec", "--crop", "art", "--out", "../x.png"], ["detect", "https://example.com"],
                  # the Code reviewer's run-gate words on b97db94: the design commands take no words, context only
                  # --target in the project, build-phase start needs --direction, the live server's port is digits,
                  # the close file is a project path, and a value with a glob character is the developer's
                  ["generate", "--anything", "https://x"], ["init", "--force", "/etc"], ["context", "--target", "/etc"],
                  ["document", "--out", "/tmp/x"], ["build-phase", "start"], ["live-server", "--port", "0.0.0.0"],
                  ["critique-storage", "close", "a", "../../b"], ["live", "--target", "src/*"],
                  ["live-complete", "--id", "../../x"])


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

    def run_as(self, args, developer, cwd=None):
        # impeccable.run with the words the engine would get, in or outside the developer's terminal, in the project
        # or in `cwd` -> (exit code, stderr, the engine's argv or None)
        if self.out.exists():
            self.out.unlink()
        with mock.patch.object(core, "set_by_developer", lambda *a, **k: developer), \
                mock.patch.dict(os.environ, {"FAKE_ENGINE_OUT": str(self.out)}), \
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


class DeveloperOnlyTests(WrapperCase):
    # The verbs that write the agents' settings, hooks or skill folders change every later session, so the command
    # runs them only in the developer's terminal (core.set_by_developer): an agent's call (here, one with Claude
    # Code's marker) runs nothing, and the developer's runs the engine. Agents get an allowlist, so any other verb,
    # hidden or added by a newer engine, is the developer's too.
    def test_an_agent_runs_only_the_allowlisted_verbs(self):
        for args in AGENT_VERBS:
            with self.subTest(allowed=args):
                rc, err, argv = self.run_as(args, developer=False)
                self.assertEqual((rc, argv), (0, args), err)
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                rc, err, argv = self.run_as(args, developer=False)
                self.assertEqual((rc, argv), (1, None), err)
                self.assertIn("change what runs in every later session", err)
        rc, err, argv = self.run_as(["polish"], developer=True)  # the developer runs any verb
        self.assertEqual((rc, argv), (0, ["polish"]), err)
        # through bin/dev: its parser drops one first `--`, so a second one reaches the engine and is refused
        rc, err, got = self.dev("--", "context", env={"CLAUDECODE": "1"})
        self.assertEqual((rc, got and got["argv"]), (0, ["context"]), err)
        self.out.unlink()
        rc, err, got = self.dev("--", "--", "context", env={"CLAUDECODE": "1"})
        self.assertEqual((rc, got), (1, None), err)

    def test_harness_verbs_run_only_in_the_developers_terminal(self):
        for args in HARNESS:
            with self.subTest(args=args):
                rc, err, got = self.dev(*args, env={"CLAUDECODE": "1"})
                self.assertEqual((rc, got), (1, None), err)
                self.assertIn("change what runs in every later session", err)
        with mock.patch.object(core, "set_by_developer", lambda *a, **k: True), \
                mock.patch.dict(os.environ, {"FAKE_ENGINE_OUT": str(self.out)}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = impeccable.run(["hooks", "on"], cwd=str(self.project))
        got = json.loads(self.out.read_text()) if self.out.exists() else {}
        self.assertEqual((rc, got.get("argv")), (0, ["hooks", "on"]), err.getvalue())


class ProjectBoundaryTests(WrapperCase):
    # The engine reads and writes in the folder it runs in and where its path values point (--target, --file,
    # --artifact, the helpers' targets, …), so an agent's call runs only in a project checkout of the workspace (its
    # git top folder below ~/Development, the handoff repository excepted), and each path value, resolved from that
    # folder through its links, stays inside that checkout (the tech lead's decisions on the Code reviewer's finding,
    # `cd ~/.claude && dev impeccable live --target settings.json` passed, and on the free words that name a file).
    # This is the check that sees the folder and the words as the engine gets them; the guard checks the typed
    # command before it.
    def handoff(self):
        # the run's handoff repository as a git checkout, for this test only
        h = core.HANDOFF
        made = not h.exists()
        if not (h / ".git").exists():
            h.mkdir(parents=True, exist_ok=True)
            self.git_init(h)
            self.addCleanup(shutil.rmtree, h if made else h / ".git", True)
        return h

    def test_an_agent_runs_the_engine_only_in_a_project_checkout(self):
        plain = core.DEV / "acme-impeccable" / "not-a-checkout"
        plain.mkdir()
        claude = core.HOME / ".claude"
        claude.mkdir(exist_ok=True)
        for here in (claude, core.HOME, plain, self.handoff(), self.box):
            for args in (["live"], ["context"], ["detect", "src"], ["live", "--target", "index.html"]):
                with self.subTest(here=str(here), args=args):
                    rc, err, argv = self.run_as(args, developer=False, cwd=here)
                    self.assertEqual((rc, argv), (1, None), err)
                    self.assertIn("not in a project checkout", err)
                    # the read verbs need a checkout too: the refusal says where to run them from
                    self.assertIn("Run it from a project folder", err)
        sub = self.project / "src"
        sub.mkdir()
        rc, err, argv = self.run_as(["live", "--target", "index.html"], developer=False, cwd=sub)
        self.assertEqual((rc, argv), (0, ["live", "--target", "index.html"]), err)
        rc, err, argv = self.run_as(["live", "--target", "settings.json"], developer=True, cwd=claude)
        self.assertEqual((rc, argv), (0, ["live", "--target", "settings.json"]), err)

    def test_an_agents_engine_paths_stay_in_the_checkout_through_links(self):
        outside = Path(tempfile.mkdtemp(dir=core.HOME))
        self.addCleanup(shutil.rmtree, outside, True)
        (self.project / "src").mkdir()
        (self.project / "src" / "out").symlink_to(outside)
        (self.project / "page.html").symlink_to(core.HOME / ".zshrc")
        for args in (["live", "--target", "src/out/index.html"], ["live", "--target=src/out"],
                     ["live", "--target", "page.html"], ["context", "--target", "src/out"],
                     ["live-wrap", "--id", "e1", "--count", "3", "--tag", "h1", "--file", "src/out/x.html"],
                     ["live-poll", "--reply", "e1", "done", "--file=src/out/x.html"]):
            with self.subTest(args=args):
                rc, err, argv = self.run_as(args, developer=False)
                self.assertEqual((rc, argv), (1, None), err)
                self.assertIn("outside the project checkout", err)
        for args in (["live", "--target", "src/index.html"], ["live-wrap", "--id", "e1", "--count", "3", "--tag",
                                                               "h1", "--file", "src/x.html"]):
            with self.subTest(allowed=args):
                self.assertEqual(self.run_as(args, developer=False)[::2], (0, args))
        rc, err, argv = self.run_as(["live", "--target", "src/out/index.html"], developer=True)
        self.assertEqual((rc, argv), (0, ["live", "--target", "src/out/index.html"]), err)

    def test_files_named_by_a_free_word_stay_in_the_checkout(self):
        # The entry file of build-phase start, the capture of record hero, and the targets of critique-storage and
        # surface-brief name files the engine reads or names its own files after: an agent gives a path in the
        # checkout, and the developer any path.
        outside = Path(tempfile.mkdtemp(dir=core.HOME))
        self.addCleanup(shutil.rmtree, outside, True)
        away = os.path.relpath(outside / "x.html", self.project)  # ../../…/x.html, out of the checkout
        for args in (["build-phase", "start", "--direction", "k1", "--artifact", away],
                     ["build-phase", "record", "hero", "--build", away],
                     ["critique-storage", "write", away, "body.md"], ["surface-brief", "read", away],
                     ["critique-storage", "trend", away, "5"]):
            with self.subTest(refused=args):
                rc, err, argv = self.run_as(args, developer=False)
                self.assertEqual((rc, argv), (1, None), err)
                self.assertIn("outside the project", err)
            with self.subTest(developer=args):
                rc, err, argv = self.run_as(args, developer=True)
                self.assertEqual((rc, argv), (0, args), err)
        # a link in the project that leads out of it passes the words, so the run checks every path value through
        # its links: these values, the related targets and the closed snapshot's file included
        (self.project / "src").mkdir()
        (self.project / "src" / "out").symlink_to(outside)
        for args in (["surface-brief", "read", "src/out/x.html"],
                     ["build-phase", "start", "--direction", "k1", "--artifact", "src/out/index.html"],
                     ["build-phase", "start", "--direction", "k1", "--decision-comp=src/out/a.webp"],
                     ["build-phase", "record", "hero", "--build", "src/out/hero.png"],
                     ["critique-storage", "write", "src/out/x.html", "body.md"],
                     ["critique-storage", "trend", "src/out/x.html", "5"],
                     ["critique-storage", "close", "src/x.html", "src/out/snap.md"],
                     ["surface-brief", "write", "src/x.html", "b.md", "src/y.html", "src/out/z.html"],
                     ["comp-spec", "--comp", "src/out/c.png", "--grid"]):
            with self.subTest(link=args):
                rc, err, argv = self.run_as(args, developer=False)
                self.assertEqual((rc, argv), (1, None), err)
                self.assertIn("outside the project checkout", err)
        # the body file is read only, and the skill writes it to a temp file: it may be anywhere
        for args in (["critique-storage", "write", "src/x.html", str(outside / "body.md")],
                     ["surface-brief", "write", "src/x.html", "src/out/b.md"]):
            with self.subTest(body=args):
                self.assertEqual(self.run_as(args, developer=False)[::2], (0, args))


if __name__ == "__main__":
    unittest.main()
