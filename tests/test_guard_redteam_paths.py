# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Writers that put a whole tree somewhere (an archive extracted, a folder copied, a patch applied) decide what lands
# below their destination. When the guard cannot read what they write (an archive it cannot list, a patch from the
# command's input), a recursive writer whose destination is or holds a protected root (devkit's state, configuration
# and install, the agents' transcripts, settings and Codex home, the handoff repository) is refused; when it can, the
# members decide, so a clean archive or tree lands in ~/Development and one that reaches a protected root does not.
# A write through a path an earlier command of the same line makes (a link, or the destination of a tree the guard
# could not read) is refused: the guard judges the path before that command runs, when the link is not there yet. So
# is a write whose path the guard cannot place, once the line made something.
# Run: python3 -m unittest tests.test_guard_redteam_paths -v
import io
import os
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}


def write_tar(path, members, mode="w"):
    """A tar archive of (name, text) files and (name, "->target") symbolic links."""
    with tarfile.open(path, mode) as tf:
        for name, text in members:
            info = tarfile.TarInfo(name)
            if text.startswith("->"):
                info.type, info.linkname = tarfile.SYMTYPE, text[2:]
                tf.addfile(info)
            else:
                info.size = len(text.encode())
                tf.addfile(info, io.BytesIO(text.encode()))


class Case(unittest.TestCase):

    def setUp(self):
        self.project = core.DEV / "acme" / "widget"
        (self.project / "src").mkdir(parents=True, exist_ok=True)
        (self.project / "out").mkdir(exist_ok=True)
        self.handoff, self.claude, self.codex = core.HANDOFF, core.CLAUDE_HOME, core.CODEX_HOME
        for folder in (self.handoff, self.claude / "projects", self.codex / "sessions"):
            folder.mkdir(parents=True, exist_ok=True)
        # archives the guard lists: nothing in them reaches a protected root (x.tar and x.zip stay missing: unread)
        clean = [("app/main.py", "print('hi')\n"), ("README.md", "x\n")]
        write_tar(self.project / "ok.tar", clean)
        write_tar(self.project / "ok.tgz", clean, "w:gz")
        with zipfile.ZipFile(self.project / "ok.zip", "w") as zf:
            for name, text in clean:
                zf.writestr(name, text)
        for gone in ("x.tar", "x.tgz", "x.zip"):
            (self.project / gone).unlink(missing_ok=True)

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
        # x.tar and x.zip do not exist, so the guard cannot list them
        self.refused(["tar -xf x.tar -C ~", "tar -xf x.tar -C ~/.claude", "tar -xf x.tar -C ~/.codex",
                      f"tar -xf x.tar -C {self.handoff}", "tar -xf x.tar -C ~/.claude/projects",
                      "tar -xzf x.tgz --directory ~/.local", "tar --directory=~/.claude -xf x.tar",
                      "bsdtar -xf x.tar -C ~/.claude", "tar xf x.tar -C ~", "gtar -x -f x.tar -C ~/.codex",
                      "tar -xvf x.tar -C ~/Development",
                      "unzip -o x.zip -d ~/.claude/projects", "unzip x.zip -d ~", f"unzip -q -d{self.claude} x.zip",
                      "ditto -x -k x.zip ~/.claude", "cpio -p ~/.codex < list.txt", "tar --dir ~/.claude -xf x.tar",
                      "xar -xf x.xar -C ~/.claude", "unar -o ~/.codex x.zip"], says="tree")
        # an extraction into a folder of the project, a listing, and an archive made from a protected root still run
        self.runs(["tar -xf ok.tar -C out", "tar -xzf ok.tgz", "tar xzf ok.tgz -C out", "unzip -o ok.zip -d out",
                   "unzip -l x.zip", "unzip -p x.zip a.txt", "ditto -x -k ok.zip out",
                   "tar -czf out/backup.tgz -C ~/.claude projects", "tar -tf x.tar -C ~", "tar -cf - -C ~ .codex",
                   "pax -w -f out/x.pax src"])

    def test_a_listed_archive_or_tree_lands_where_its_members_do(self):
        # in ~/Development, which holds the handoff repository, a clean archive or tree lands; one that reaches the
        # handoff repository does not
        write_tar(self.project / "reach.tar", [(f"{self.handoff.name}/projects/x.md", "x\n")])
        self.runs(["tar -xf ok.tar -C ~/Development", "cp -R src ~/Development/", "unzip -o ok.zip -d ~/Development"])
        self.refused(["tar -xf reach.tar -C ~/Development"], says="would put")

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
        # the sources do not exist, so the guard cannot walk them
        self.refused(["cp -R gone ~/.claude/projects/", "cp -R gone/. ~/.claude/", "cp -a gone ~", "cp -r gone ~/.codex",
                      "cp -Rf gone ~/.claude", "gcp --recursive gone ~/.claude", "rsync -a gone/ ~/.claude/",
                      "rsync -r gone ~/Development/", "rsync -av --delete gone/ ~/.local/", "ditto gone ~/.claude",
                      "scp -r gone ~/.codex", f"cp -R gone {self.handoff}",
                      "git apply --unsafe-paths --directory=~/.claude/projects p.patch",
                      "git checkout-index -a --prefix=~/.claude/", "rsync -d gone/ ~/.claude/",
                      "cp --parents gone ~/.codex/", "rsync -a gone/ localhost:.claude/"], says="tree")
        # a tree copied or patched inside the project, a single file into the home folder, and a copy out of a
        # protected root still run
        self.runs(["cp -R src out/", "cp -a src/. out/", "rsync -a src/ out/", "ditto src out", "pax -rw src out",
                   "ditto -c -k --keepParent src out/x.zip", "git apply p.patch", "git apply --directory=src p.patch",
                   "git apply --check p.patch", "git checkout-index -a --prefix=out/", "cp notes.txt ~/",
                   "cp -R ~/.claude/projects out/", "rsync -a ~/.codex/sessions/ out/"])


class WritesThroughWhatTheLineMakes(Case):

    def setUp(self):
        super().setUp()
        p = self.project
        write_tar(p / "links.tar", [("st", "->../../../../.local/state")])
        with zipfile.ZipFile(p / "links.zip", "w") as zf:
            info = zipfile.ZipInfo("st")
            info.create_system, info.external_attr = 3, 0o120777 << 16
            zf.writestr(info, "../../../../.local/state")
        for folder in ("D", "src"):
            (p / folder).mkdir(exist_ok=True)
            if not os.path.lexists(p / folder / "st"):
                os.symlink("../../../../.local/state", p / folder / "st")
        (p / ".claude").mkdir(exist_ok=True)
        if not os.path.lexists(p / "L"):
            os.symlink(".claude", p / "L")  # the project's settings folder, which a move does not resolve as protected
        (p / "p.patch").write_text("diff --git a/a.txt b/a.txt\nnew file mode 100644\n--- /dev/null\n+++ b/a.txt\n"
                                   "@@ -0,0 +1 @@\n+x\n")
        (p / "links.patch").write_text("diff --git a/S b/S\nnew file mode 120000\n--- /dev/null\n+++ b/S\n"
                                       "@@ -0,0 +1 @@\n+../../../../.local/state\n\\ No newline at end of file\n")

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
                      "unzip -o links.zip -d S; cp f.txt S/st/x", "cp -R D S && dev rm S/st/x",
                      "rsync -a D/ S/ && tee S/st/x < f.txt", "cp -R src out/ && echo x > out/src/st/x",
                      "tar -xf links.tar && sed -i '' s/a/b/ st/runs/r.json",
                      "git apply links.patch && echo x > S/devkit/r",
                      "mv L M && echo x > M/settings.json", "gmv L M; echo x > M/settings.json"], says="makes")
        # reads of what it makes, removing the made folder itself, and writes elsewhere or before it still run
        self.runs(["tar -xf ok.tar -C S && ls S/st", "ln -s a b && cat b", "tar -xf ok.tar -C S && dev rm S",
                   "unzip -o ok.zip -d S && grep -r foo S", "mkdir -p S && tar -xf ok.tar -C S",
                   "cp a.txt b.txt && echo x >> b.txt", "mv a.txt b.txt && echo x >> b.txt",
                   "cp -R src out/ && echo x > out/notes.txt",
                   "echo x > S/a.txt && tar -xf ok.tar -C out"])

    def test_what_the_guard_lists_makes_only_its_links(self):
        # an archive or a patch with no link in it makes no path a later write may not go through
        self.runs(["tar -xzf ok.tgz && echo ok > status.txt", "tar -xf ok.tar -C S && echo x > S/app/notes.md",
                   "git apply p.patch && echo x > notes.txt", "cp -R D S && echo x > S/notes.md"])

    def test_a_write_the_guard_cannot_place_after_the_line_makes_a_path_is_refused(self):
        up = "../../../../.local/state"
        self.refused(['cd "$TMPDIR" && ln -s ~/.claude L && echo x > L/settings.json',
                      'cd "$(mktemp -d)" && ln -s ~/.claude L && echo x > L/settings.json',
                      'ln -s ~/.claude "$D/L" && echo x > "$D/L/x"'])
        self.refused([f'cd "$TMPDIR" && ln -s {up} L && echo x > L/devkit/runs/r.json',
                      f'cd "$(mktemp -d)" && ln -s {up} L && echo x > L/devkit/runs/r.json',
                      f'ln -s {up} "$D/L" && echo x > "$D/L/devkit/r"', f'ln -s {up} sub/L && echo x > "$D/r"',
                      'mv "$LINK" M && echo x > M/settings.json'], says="earlier command")
        # with nothing made, the same writes are judged as any write
        self.runs(['cd "$TMPDIR" && echo x > notes.txt', 'echo x > "$D/notes.txt"'])


if __name__ == "__main__":
    unittest.main()
