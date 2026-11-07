# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The impeccable engine, fetched by the skills step of `dev install` instead of at the skill's first run
# (lib/devkit/setup.py): the engine for this platform comes from the upstream release that matches the skill's
# scripts/VERSION, is checked against the pinned skill's own engine-checksums.txt, and lands beside the launcher as
# scripts/bin/<os>-<arch>/impeccable (0755), with an audit row and a record the guard reads. A mismatch or a failed
# download leaves nothing executable and is reported, not fatal. `dev doctor` reports the skill, the engine and the
# Codex plugin. Everything runs in a temporary HOME with a made-up engine; the download is faked at its boundary
# (setup._download_engine), so no test reaches the network. Run: python3 -m unittest tests.test_skill_engine -v
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, core, guard, setup  # noqa: E402

PLATFORM = "darwin-arm64"
ENGINE = b"\xcf\xfa\xed\xfe made-up engine for the tests\n"
DIGEST = hashlib.sha256(ENGINE).hexdigest()
LAUNCHER = "#!/bin/sh\nexec \"$(dirname \"$0\")/bin/$PLATFORM/impeccable\" \"$@\"\n"
URL = "https://github.com/pbakaus/impeccable/releases/download/engine-v0.1.18/impeccable-darwin-arm64"
CODEX_LISTED = "impeccable@openai-curated-remote  installed, enabled\nother@x  installed, enabled\n"


def make_skill(folder: Path, digest: str = DIGEST) -> Path:
    # The parts of the pinned skill the engine step reads: the launcher, scripts/VERSION and engine-checksums.txt.
    scripts = folder / "scripts"
    scripts.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: impeccable\n---\n")
    (scripts / "impeccable").write_text(LAUNCHER)
    (scripts / "impeccable").chmod(0o755)
    (scripts / "VERSION").write_text("0.1.18\n")
    (scripts / "engine-checksums.txt").write_text(
        "# Engine binary digests: <engine-version> <asset> <sha256>.\n"
        f"0.1.18 impeccable-{PLATFORM} {digest}\n"
        f"0.1.18 impeccable-linux-x64 {'0' * 64}\n")
    return folder


class EngineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.state = self.home / ".local/state/devkit"
        self.share = self.home / ".local/share/devkit/skills"
        self.state.mkdir(parents=True)
        self.urls = []
        self.serve = ENGINE
        for p in (mock.patch.object(setup, "STATE", self.state),
                  mock.patch.object(setup, "SKILLS_SHARE", self.share),
                  mock.patch.object(setup, "ENGINES_SHARE", self.home / ".local/share/devkit/engines"),
                  mock.patch.object(setup, "CLAUDE_HOME", self.home / ".claude"),
                  mock.patch.object(setup, "AGENTS_HOME", self.home / ".agents"),
                  mock.patch.object(setup, "engine_platform", lambda: PLATFORM),
                  mock.patch.object(setup, "_download_engine", self.download),
                  mock.patch.object(audit, "AUDIT", self.state / "audit")):
            p.start()
            self.addCleanup(p.stop)
        self.skill = make_skill(self.share / "impeccable")
        self.bin = self.skill / "scripts/bin" / PLATFORM / "impeccable"

    def download(self, url, dest):
        # the network boundary: serve the bytes this test chose, or fail like an unreachable host
        self.urls.append(url)
        if isinstance(self.serve, Exception):
            raise self.serve
        Path(dest).write_bytes(self.serve)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return setup.install_impeccable_engine(self.skill, "pbakaus/impeccable")

    def audits(self, action="install.engine"):
        return [r for f in sorted((self.state / "audit").glob("*.jsonl")) for ln in f.read_text().splitlines()
                for r in [json.loads(ln)] if r["action"] == action]

    def record(self):
        f = self.state / setup.ENGINES_FILE
        return (json.loads(f.read_text()) if f.exists() else {}).get("impeccable")

    def executables(self):
        roots = [self.skill / "scripts/bin", setup.ENGINES_SHARE]
        return sorted(str(p) for r in roots if r.exists() for p in r.rglob("*") if p.is_file())


class InstallTests(EngineCase):
    def test_a_matching_digest_installs_the_engine_beside_the_launcher(self):
        res = self.install()
        self.assertEqual(res["outcome"], "installed")
        self.assertEqual(self.urls, [URL])  # the asset only: no digest is ever taken from the release
        self.assertEqual(self.bin.read_bytes(), ENGINE)
        self.assertEqual(self.bin.stat().st_mode & 0o777, 0o755)
        [row] = self.audits()
        self.assertEqual((row["target"], row["outcome"]), ("impeccable", "ok"))
        self.assertIn(DIGEST[:12], row["detail"])
        rec = self.record()
        self.assertEqual((rec["sha256"], rec["engine"], rec["platform"]), (DIGEST, str(self.bin), PLATFORM))
        self.assertEqual(rec["launcher_sha256"], hashlib.sha256(LAUNCHER.encode()).hexdigest())

    def test_a_mismatched_digest_leaves_nothing_executable(self):
        self.serve = b"not the pinned engine"
        res = self.install()
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("does not match", res["detail"])
        self.assertEqual(self.executables(), [])
        self.assertEqual([r["outcome"] for r in self.audits()], ["blocked"])  # the audit's word for a refusal
        self.assertIsNone(self.record())

    def test_a_failed_download_leaves_nothing_and_is_reported(self):
        self.serve = OSError("network is unreachable")
        res = self.install()  # returns: the rest of the install goes on
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("network is unreachable", res["detail"])
        self.assertIn("dev install --only skills", res["detail"])
        self.assertEqual(self.executables(), [])
        self.assertIsNone(self.record())

    def test_a_reinstalled_skill_gets_its_verified_engine_without_a_download(self):
        self.install()
        shutil.rmtree(self.skill)  # what the skills step does to every skill copy before it copies it again
        make_skill(self.skill)
        self.serve = AssertionError("downloaded again")
        res = self.install()
        self.assertEqual(res["outcome"], "present")
        self.assertEqual(self.urls, [URL])
        self.assertEqual(self.bin.read_bytes(), ENGINE)
        self.assertEqual(self.record()["engine"], str(self.bin))

    def test_a_kept_engine_that_changed_is_downloaded_again(self):
        self.install()
        shutil.rmtree(self.skill)
        make_skill(self.skill)
        kept = [p for p in setup.ENGINES_SHARE.rglob("*") if p.is_file()]
        self.assertEqual(len(kept), 1)
        kept[0].write_bytes(b"tampered")
        res = self.install()
        self.assertEqual(res["outcome"], "installed")
        self.assertEqual(self.urls, [URL, URL])
        self.assertEqual(self.bin.read_bytes(), ENGINE)

    @unittest.skipIf(os.geteuid() == 0, "root writes through folder permissions")
    def test_an_unwritable_engines_folder_fails_without_raising(self):
        setup.ENGINES_SHARE.mkdir(parents=True)
        setup.ENGINES_SHARE.chmod(0o500)  # the kept copy's folder cannot be created
        self.addCleanup(setup.ENGINES_SHARE.chmod, 0o755)
        res = self.install()
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("Permission denied", res["detail"])
        self.assertIn("engines/impeccable", res["detail"])  # the folder to fix
        self.assertIn("dev install --only skills", res["detail"])
        self.assertEqual(self.executables(), [])
        self.assertIsNone(self.record())
        self.assertEqual([r["outcome"] for r in self.audits()], ["failed"])

    def test_a_folder_where_the_engine_goes_fails_without_raising(self):
        self.bin.mkdir(parents=True)
        res = self.install()
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("Is a directory", res["detail"])
        self.assertTrue(self.bin.is_dir())  # left for the developer to look at, never removed with its content
        self.assertIsNone(self.record())

    def test_a_platform_without_a_pinned_digest_downloads_nothing(self):
        with mock.patch.object(setup, "engine_platform", lambda: "linux-riscv64"):
            res = self.install()
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("no digest for impeccable-linux-riscv64", res["detail"])
        self.assertEqual(self.urls, [])
        self.assertEqual(self.executables(), [])

    def test_the_skills_step_installs_the_engine_with_the_skill(self):
        src = Path(tempfile.mkdtemp(dir=self.home))
        make_skill(src / ".claude/skills/impeccable")
        manifest = {"sources": [{"repo": "pbakaus/impeccable", "ref": "414603bf14c7a344edb466bb1f85690c77ce8b7d",
                                 "license": "Apache-2.0", "agents": ["claude"],
                                 "skills": {"impeccable": ".claude/skills/impeccable"}}]}
        shutil.rmtree(self.skill)
        tree = self.home / "devkit"  # devkit's own skills folder, empty here (git keeps no empty folder)
        (tree / "agents/skills").mkdir(parents=True)
        with mock.patch.object(setup, "load_conf", lambda name: manifest), \
                mock.patch.object(setup, "_fetch_source", lambda s: src), \
                mock.patch.object(setup, "DEVKIT_HOME", tree), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            setup.install_skills()
        self.assertEqual(self.bin.read_bytes(), ENGINE)
        self.assertIn("impeccable engine 0.1.18", out.getvalue())
        self.assertTrue((setup.CLAUDE_HOME / "skills/impeccable/scripts/bin" / PLATFORM / "impeccable").is_file())

    @unittest.skipIf(os.geteuid() == 0, "root writes through folder permissions")
    def test_the_skills_step_links_its_other_skills_when_the_engine_fails(self):
        src = Path(tempfile.mkdtemp(dir=self.home))
        make_skill(src / ".claude/skills/impeccable")
        (src / "skills/later").mkdir(parents=True)  # a skill of a source after impeccable's
        (src / "skills/later/SKILL.md").write_text("---\nname: later\n---\n")
        manifest = {"sources": [
            {"repo": "pbakaus/impeccable", "ref": "414603bf14c7a344edb466bb1f85690c77ce8b7d", "license": "Apache-2.0",
             "agents": ["claude"], "skills": {"impeccable": ".claude/skills/impeccable"}},
            {"repo": "example/later", "ref": "0" * 40, "license": "MIT", "skills": {"later": "skills/later"}}]}
        shutil.rmtree(self.skill)
        tree = self.home / "devkit"
        (tree / "agents/skills").mkdir(parents=True)
        setup.ENGINES_SHARE.mkdir(parents=True)
        setup.ENGINES_SHARE.chmod(0o500)
        self.addCleanup(setup.ENGINES_SHARE.chmod, 0o755)
        with mock.patch.object(setup, "load_conf", lambda name: manifest), \
                mock.patch.object(setup, "_fetch_source", lambda s: src), \
                mock.patch.object(setup, "DEVKIT_HOME", tree), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            setup.install_skills()
        self.assertIn("impeccable engine: ", out.getvalue())
        for home in (setup.CLAUDE_HOME, setup.AGENTS_HOME):
            with self.subTest(home=str(home)):
                self.assertTrue((home / "skills/later/SKILL.md").is_file())
        self.assertTrue((setup.CLAUDE_HOME / "skills/impeccable/SKILL.md").is_file())


class ProtectedLocationTests(unittest.TestCase):
    # The guard trusts the launcher by what the skills step records, so the record and the engine copies must be out
    # of an agent's reach (Security engineer, M486): a write to any of them is refused, whether the path comes from
    # devkit's own constants (the run's temporary home) or is typed out.
    def test_the_engine_its_record_and_its_kept_copy_are_refused_to_agents(self):
        made = [setup.SKILLS_SHARE / "impeccable/scripts/bin" / PLATFORM / "impeccable",
                setup.SKILLS_SHARE / "impeccable/scripts/impeccable",
                core.STATE / setup.ENGINES_FILE,
                setup.ENGINES_SHARE / "impeccable/0.1.18" / f"impeccable-{PLATFORM}"]
        typed = ["~/.local/share/devkit/skills/impeccable/scripts/bin/darwin-arm64/impeccable",
                 f"~/.local/state/devkit/{setup.ENGINES_FILE}",
                 "~/.local/share/devkit/engines/impeccable/0.1.18/impeccable-darwin-arm64"]
        for path in [str(p) for p in made] + typed:
            with self.subTest(path=path):
                v = guard.check("Write", {"file_path": path, "content": "x"}, str(ROOT), "claude")
                self.assertEqual((v or ("allow",))[0], "deny")
                v = guard.check("Bash", {"command": f"cp /tmp/x {path}"}, str(ROOT), "claude")
                self.assertEqual((v or ("allow",))[0], "deny")


class DoctorTests(EngineCase):
    def setUp(self):
        super().setUp()
        (setup.CLAUDE_HOME / "skills").mkdir(parents=True)
        (setup.CLAUDE_HOME / "skills/impeccable").symlink_to(self.skill)

    def rows(self):
        return {name: (ok, detail, fix) for _, name, ok, detail, fix in setup.impeccable_checks()}

    def test_a_skill_with_its_verified_engine_passes(self):
        self.install()
        rows = self.rows()
        self.assertEqual(rows["impeccable skill (Claude)"][0], True)
        ok, detail, _ = rows["impeccable engine"]
        self.assertTrue(ok)
        self.assertIn(f"0.1.18 for {PLATFORM}", detail)
        self.assertIn("matches engine-checksums.txt", detail)

    def test_a_missing_engine_fails_with_the_install_fix(self):
        ok, detail, fix = self.rows()["impeccable engine"]
        self.assertFalse(ok)
        self.assertIn("not installed", detail)
        self.assertEqual(fix, "dev install --only skills")

    def test_an_engine_that_differs_from_the_pinned_digest_fails(self):
        self.install()
        self.bin.write_bytes(b"swapped engine")
        ok, detail, fix = self.rows()["impeccable engine"]
        self.assertFalse(ok)
        self.assertIn("does not match engine-checksums.txt", detail)
        self.assertEqual(fix, "dev install --only skills")

    def test_a_skill_missing_for_claude_fails_with_the_install_fix(self):
        (setup.CLAUDE_HOME / "skills/impeccable").unlink()
        ok, detail, fix = self.rows()["impeccable skill (Claude)"]
        self.assertFalse(ok)
        self.assertIn("not linked", detail)
        self.assertEqual(fix, "dev install --only skills")

    def test_the_codex_plugin_row_follows_codex_plugin_list(self):
        group, name, ok, detail, fix = setup.impeccable_codex_check(CODEX_LISTED)
        self.assertEqual((group, name, ok), ("Codex", "impeccable plugin", True))
        _, _, ok, detail, fix = setup.impeccable_codex_check("impeccable@openai-curated-remote  installed, disabled\n")
        self.assertFalse(ok)
        self.assertIn("not enabled", detail)
        self.assertEqual(fix, "dev install --only codex")


if __name__ == "__main__":
    unittest.main()
