# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Writers that put a whole tree somewhere (an archive extracted, a folder copied, a patch applied) decide what lands
# below their destination, and the guard cannot see an archive's members or the links among them. So a recursive
# writer whose destination is or holds a protected root (devkit's state, configuration and install, the agents'
# transcripts, settings and Codex home, the handoff repository) is refused, and so is a write through a path an earlier
# command of the same line makes (a link, or what an archive or a tree copy leaves there): the guard judges the path
# before that command runs, when the link is not there yet.
# Run: python3 -m unittest tests.test_guard_redteam_paths -v
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}


class Case(unittest.TestCase):

    def setUp(self):
        self.project = core.DEV / "acme" / "widget"
        (self.project / "src").mkdir(parents=True, exist_ok=True)
        (self.project / "out").mkdir(exist_ok=True)
        self.handoff, self.claude, self.codex = core.HANDOFF, core.CLAUDE_HOME, core.CODEX_HOME
        for folder in (self.handoff, self.claude / "projects", self.codex):
            folder.mkdir(parents=True, exist_ok=True)

    def verdict(self, cmd, cwd=None):
        with mock.patch.object(core, "load_config", return_value=ENFORCE):
            v = guard.check("Bash", {"command": cmd}, str(cwd or self.project), "claude")
        return v[1] if v and v[0] == "deny" else None

    def refused(self, cmds, cwd=None, says=None):
        for cmd in cmds:
            with self.subTest(cmd=cmd, cwd=str(cwd or "")):
                why = self.verdict(cmd, cwd)
                self.assertIsNotNone(why)
                if says:
                    self.assertIn(says, why)

    def runs(self, cmds, cwd=None):
        for cmd in cmds:
            with self.subTest(cmd=cmd, cwd=str(cwd or "")):
                self.assertIsNone(self.verdict(cmd, cwd))


class RecursiveWriterDestinations(Case):

    def test_an_archive_extracted_into_a_folder_that_is_or_holds_a_protected_root_is_refused(self):
        self.refused(["tar -xf x.tar -C ~", "tar -xf x.tar -C ~/.claude", "tar -xf x.tar -C ~/.codex",
                      f"tar -xf x.tar -C {self.handoff}", "tar -xf x.tar -C ~/.claude/projects",
                      "tar -xzf x.tgz --directory ~/.local", "tar --directory=~/.claude -xf x.tar",
                      "bsdtar -xf x.tar -C ~/.claude", "tar xf x.tar -C ~", "gtar -x -f x.tar -C ~/.codex",
                      "tar -xvf x.tar -C ~/Development",
                      "unzip -o x.zip -d ~/.claude/projects", "unzip x.zip -d ~", f"unzip -q -d{self.claude} x.zip",
                      "ditto -x -k x.zip ~/.claude", "cpio -p ~/.codex < list.txt"], says="tree")
        # an extraction into a folder of the project, a listing, and an archive made from a protected root still run
        self.runs(["tar -xf x.tar -C out", "tar -xzf x.tgz", "tar xzf x.tgz -C out", "unzip -o x.zip -d out",
                   "unzip -l x.zip", "unzip -p x.zip a.txt", "ditto -x -k x.zip out",
                   "tar -czf out/backup.tgz -C ~/.claude projects", "tar -tf x.tar -C ~", "tar -cf - -C ~ .codex",
                   "pax -w -f out/x.pax src"])

    def test_an_archive_extracted_in_a_folder_that_holds_a_protected_root_is_refused(self):
        # with no -C or -d, the members land in the folder the command runs in
        self.refused(["tar -xf /tmp/x.tar", "unzip -o /tmp/x.zip", "cpio -i < /tmp/x.cpio", "cpio -idv < /tmp/x.cpio",
                      "cd ~/.claude && tar -xf /tmp/x.tar"], cwd=core.HOME, says="tree")

    def test_members_that_may_name_any_path_are_refused_wherever_they_land(self):
        # absolute paths or `..` kept: the destination folder does not bound what is written
        self.refused(["tar -xPf x.tar -C out", "tar --absolute-names -xf x.tar", "unzip -: x.zip -d out",
                      "cpio -i --insecure < x.cpio", "pax -r -f x.pax", "pax -rv < x.pax",
                      "git apply --unsafe-paths p.patch"], says="anywhere")

    def test_a_tree_copied_into_a_folder_that_is_or_holds_a_protected_root_is_refused(self):
        self.refused(["cp -R src ~/.claude/projects/", "cp -R src/. ~/.claude/", "cp -a src ~", "cp -r src ~/.codex",
                      "cp -Rf src ~/.claude", "gcp --recursive src ~/.claude", "rsync -a src/ ~/.claude/",
                      "rsync -r src ~/Development/", "rsync -av --delete src/ ~/.local/", "ditto src ~/.claude",
                      "scp -r src ~/.codex", f"cp -R src {self.handoff}",
                      "git apply --unsafe-paths --directory=~/.claude/projects p.patch",
                      "git checkout-index -a --prefix=~/.claude/"], says="tree")
        # a tree copied or patched inside the project, a single file into the home folder, and a copy out of a
        # protected root still run
        self.runs(["cp -R src out/", "cp -a src/. out/", "rsync -a src/ out/", "ditto src out", "pax -rw src out",
                   "ditto -c -k --keepParent src out/x.zip", "git apply p.patch", "git apply --directory=src p.patch",
                   "git apply --check p.patch", "git checkout-index -a --prefix=out/", "cp notes.txt ~/",
                   "cp -R ~/.claude/projects out/", "rsync -a ~/.codex/sessions/ out/"])


class WritesThroughWhatTheLineMakes(Case):

    def test_a_write_below_a_path_an_earlier_command_makes_is_refused(self):
        # a link's text is read from the link's own folder, so sub/L leads to ~/.local/state from this project, while
        # the same text read from the project's folder leads one level above the home folder
        up = "../../../../.local/state"
        self.refused(["tar -xf links.tar -C S && echo x >> S/st/runs/r.json",
                      f"ln -s {up} sub/L && echo x > sub/L/devkit/runs/r.json",
                      f"ln -s {up} sub/L; cd sub/L/devkit && echo x > r.json",
                      f"ln -s {up} sub/L && mv sub/L M && echo x > M/devkit/r",
                      f"bash -c 'ln -s {up} sub/L' && echo x > sub/L/devkit/r",
                      f"ln -s {up}/devkit/runs sub/L && echo x > sub/L/../r",
                      "unzip -o x.zip -d S; cp f.txt S/st/x", "cp -R D S && dev rm S/st/x",
                      "rsync -a D/ S/ && tee S/st/x < f.txt", "cp -R src out/ && echo x > out/src/st/x",
                      "tar -xf links.tar && sed -i '' s/a/b/ st/runs/r.json"], says="makes")
        # reads of what it makes, removing the made folder itself, and writes elsewhere or before it still run
        self.runs(["tar -xf x.tar -C S && ls S/st", "ln -s a b && cat b", "tar -xf x.tar -C S && dev rm S",
                   "unzip -o x.zip -d S && grep -r foo S", "mkdir -p S && tar -xf x.tar -C S",
                   "cp a.txt b.txt && echo x >> b.txt", "mv a.txt b.txt && echo x >> b.txt",
                   "cp -R src out/ && echo x > out/notes.txt",
                   "echo x > S/a.txt && tar -xf x.tar -C out"])


if __name__ == "__main__":
    unittest.main()
