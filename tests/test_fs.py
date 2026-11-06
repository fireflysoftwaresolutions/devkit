# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""File commands: dev rm, mv, cp, files, find, grep, diff. Run: python3 -m unittest tests.test_fs -v

Every test works in a throwaway workspace (a fake ~/Development with real git repositories in a temp dir)
and replaces the Trash with a folder of that workspace, so the user's files and Trash are never touched.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, fs, guard, ui  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout


def parser():
    ap = argparse.ArgumentParser(prog="dev")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_, **kw):  # the same contract as cli.build_parser's add()
        p = sp.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(fn=fn)
        return p

    fs.register(add)
    return ap


def dev(*argv):
    """Run a dev file command. -> (rc, stdout, stderr), ANSI stripped."""
    a = parser().parse_args([str(x) for x in argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = a.fn(a)
    return rc, ui.ANSI_RE.sub("", out.getvalue()), ui.ANSI_RE.sub("", err.getvalue())


class Workspace:
    """A fake dev root with one repository, devkit state, and a Trash folder, all in a temp dir.

    Only <dev>/.tmp counts as a temp root, so the workspace behaves like ~/Development even though it
    lives under /var/folders."""

    PATCHED = [(core, "DEV"), (guard, "DEV"), (core, "HANDOFF"), (core, "WT_ROOT"), (core, "LOCAL"),
               (core, "TMP_ROOT"), (core, "STATE"), (core, "owners"), (fs, "is_temp"), (fs, "trash_paths"),
               (fs, "USE_FD")]

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(core.real(self.tmp.name))
        self.dev = self.base / "Development"
        self.repo = self.dev / "acme" / "widget"
        self.trash = self.base / "Trash"
        self.outside = self.base / "outside"
        for d in (self.repo, self.dev / ".tmp", self.dev / ".handoff" / "items", self.trash, self.base / "state",
                  self.outside):
            d.mkdir(parents=True)
        self.saved = [(m, n, getattr(m, n)) for m, n in self.PATCHED]
        core.DEV = guard.DEV = self.dev
        core.HANDOFF, core.WT_ROOT = self.dev / ".handoff", self.dev / ".worktrees"
        core.LOCAL, core.TMP_ROOT, core.STATE = self.dev / "_local", self.dev / ".tmp", self.base / "state"
        core.owners = lambda: {"acme"}
        fs.is_temp = lambda p: fs._under(p, str(core.TMP_ROOT))
        fs.trash_paths = self.fake_trash
        fs.USE_FD = False
        self.trashed = []
        self.old_cwd = os.getcwd()
        self.make_repo(self.repo)
        os.chdir(self.repo)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old_cwd)
        for m, n, v in self.saved:
            setattr(m, n, v)
        self.tmp.cleanup()

    def fake_trash(self, paths):
        res = {}
        for p in paths:
            if not fs._under(p, str(self.base)):  # safety net: a test must never reach real files
                raise AssertionError(f"test tried to trash {p}")
            dest = self.trash / f"{len(self.trashed)}-{os.path.basename(p)}"
            shutil.move(p, str(dest))
            self.trashed.append(p)
            res[p] = (str(dest), None)
        return res

    @staticmethod
    def make_repo(path, files=None):
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q", "-b", "main")
        files = files or {
            ".gitignore": "node_modules/\ndist/\n.env\n*.log\n",
            "README.md": "# widget\n",
            "src/app.py": "".join(f"def f{i}():\n    return {i}\n" for i in range(5)),
            "src/util.py": "VALUE = 1\n",
            "package-lock.json": "".join(f'  "dep{i}": "1.0.{i}",\n' for i in range(50)),
        }
        for rel, text in files.items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
        return path

    def write(self, rel, text="x\n", root=None):
        f = (root or self.repo) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f


def holding(path, cwd=None):
    """A child process that keeps `path` open (or works inside `cwd`) until killed."""
    code = "import sys,time; f=open(sys.argv[1]) if sys.argv[1] != '-' else None; print('ready', flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code, str(path) if path else "-"], cwd=cwd,
                             stdout=subprocess.PIPE, text=True)
    child.stdout.readline()
    return child


def stop(child):
    child.kill()
    child.wait()
    child.stdout.close()


class RmTests(unittest.TestCase):
    def test_untracked_needs_discard_then_trashes_and_undo_restores(self):
        with Workspace() as w:
            f = w.write("notes.txt", "a\nb\n")
            rc, out, err = dev("rm", "notes.txt")
            self.assertEqual(rc, 1)
            self.assertIn("notes.txt: untracked, it exists only on disk (2 lines) would be lost", err)
            self.assertIn("retry with --discard", err)
            self.assertIn("nothing was deleted", err)
            self.assertTrue(f.exists())
            rc, out, _ = dev("rm", "--discard", "notes.txt", "--why", "scratch")
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "trashed notes.txt (4 B) · undo: dev rm --undo")
            self.assertFalse(f.exists())
            rec = json.loads((core.STATE / "trash.jsonl").read_text().splitlines()[-1])
            self.assertEqual((rec["path"], rec["kind"], rec["cwd"]), (str(f), "untracked", str(w.repo)))
            self.assertIn("scratch", rec["reason"])
            rc, out, _ = dev("rm", "--undo")
            self.assertEqual(rc, 0)
            self.assertIn("restored 1 item", out)
            self.assertEqual(f.read_text(), "a\nb\n")
            self.assertEqual(dev("rm", "--undo")[0], 1)  # nothing left to undo

    def test_modified_tracked_file_shows_what_would_be_lost(self):
        with Workspace() as w:
            w.write("src/util.py", "VALUE = 2\nOTHER = 3\n")
            rc, _, err = dev("rm", "src/util.py")
            self.assertEqual(rc, 1)
            self.assertIn("tracked by git", err)
            self.assertIn("uncommitted changes (+2 -1) would be lost", err)
            self.assertIn("retry with --tracked --discard", err)
            rc, _, err = dev("rm", "--tracked", "src/util.py")
            self.assertIn("retry with --discard", err)
            self.assertNotIn("--tracked →", err)
            self.assertTrue((w.repo / "src/util.py").exists())

    def test_tracked_deletion_is_staged_and_undo_restores_the_index(self):
        with Workspace() as w:
            rc, _, err = dev("rm", "README.md")
            self.assertEqual(rc, 1)
            self.assertIn("retry with --tracked", err)
            rc, out, _ = dev("rm", "--tracked", "README.md")
            self.assertEqual(rc, 0)
            self.assertIn("1 deletion staged in git", out)
            self.assertEqual(git(w.repo, "status", "--porcelain").strip(), "D  README.md")
            dev("rm", "--undo")
            self.assertEqual(git(w.repo, "status", "--porcelain").strip(), "")

    def test_directory_needs_r_and_lists_its_losses(self):
        with Workspace() as w:
            w.write("src/new.py", "n = 1\n")
            w.write("src/app.py", "changed\n")
            rc, _, err = dev("rm", "src")
            self.assertIn("src/: a directory", err)
            self.assertIn("retry with -r --tracked --discard", err)
            self.assertIn("src/app.py | +1 -10", err)
            self.assertIn("src/new.py | untracked, 1 line", err)

    def test_gitignored_build_output_is_the_fast_path(self):
        with Workspace() as w:
            w.write("node_modules/pkg/index.js", "x" * 2048)
            rc, out, err = dev("rm", "-rf", "node_modules")
            self.assertEqual(rc, 0, err)
            self.assertRegex(out.strip(), r"^trashed node_modules/ \(\d+ KB\) · undo: dev rm --undo$")
            self.assertFalse((w.repo / "node_modules").exists())

    def test_gitignored_local_secrets_need_discard(self):
        with Workspace() as w:
            w.write(".env", "TOKEN=abc\n")
            rc, _, err = dev("rm", ".env")
            self.assertIn("a gitignored local file git cannot restore", err)
            self.assertIn("retry with --discard", err)
            w.write("debug.log", "noise\n")
            self.assertEqual(dev("rm", "debug.log")[0], 0)  # ignored, not sensitive: straight to the Trash

    def test_workspace_structure_is_never_deleted(self):
        with Workspace() as w:
            w.write("items/x.md", root=core.HANDOFF)
            flags = ["-r", "--tracked", "--discard", "--force-in-use", "--outside"]
            for path, why in ((w.dev, "the workspace root"), (w.dev / "acme", "top-level workspace folder"),
                              (w.repo, "a repository checkout"), (w.repo / ".git", "a .git directory"),
                              (core.HANDOFF, "the handoff repository"),
                              (core.HANDOFF / "items", "a managed handoff folder"),
                              (core.HANDOFF / "items" / "x.md", "managed by devkit"), (w.dev / ".tmp", "devkit temp root")):
                with self.subTest(path=path):
                    os.chdir(w.base)
                    rc, _, err = dev("rm", *flags, path)
                    self.assertEqual(rc, 1)
                    self.assertIn(why, err)
                    self.assertTrue(path.exists())
            self.assertEqual(w.trashed, [])

    def test_git_internals_are_refused_but_stale_locks_may_go(self):
        with Workspace() as w:
            rc, _, err = dev("rm", "-r", "--discard", "--tracked", ".git/objects")
            self.assertIn("inside .git: repository internals", err)
            self.assertTrue((w.repo / ".git" / "objects").exists())
            lock = w.write(".git/index.lock", "")
            rc, out, err = dev("rm", ".git/index.lock")
            self.assertEqual(rc, 0, err)
            self.assertFalse(lock.exists())

    def test_folder_containing_a_repository_is_refused(self):
        with Workspace() as w:
            Workspace.make_repo(w.dev / "acme" / "group" / "inner", {"a.txt": "a\n"})
            os.chdir(w.base)
            rc, _, err = dev("rm", "-r", "--discard", w.dev / "acme" / "group")
            self.assertIn("contains the repository", err)

    def test_outside_needs_flag_and_system_folders_never(self):
        with Workspace() as w:
            f = w.write("x.txt", root=w.outside)
            rc, _, err = dev("rm", f)
            self.assertIn("outside", err)
            self.assertIn("retry with --outside", err)
            self.assertEqual(dev("rm", "--outside", f)[0], 0)
            for path in ("/", str(core.HOME), "/System", "/usr/bin/true", str(core.HOME / ".ssh")):
                with self.subTest(path=path):
                    rc, _, err = dev("rm", "-r", "--outside", "--discard", "--tracked", "--force-in-use", path)
                    self.assertEqual(rc, 1)
                    self.assertIn("no override", err)

    def test_open_file_is_in_use(self):
        with Workspace() as w:
            f = w.write("data.txt")
            child = holding(f)
            try:
                rc, _, err = dev("rm", "--discard", f)
                self.assertEqual(rc, 1)
                self.assertRegex(err, rf"in use by \S+ \(pid {child.pid}\)")
                self.assertIn("retry with --force-in-use", err)
                self.assertEqual(dev("rm", "--discard", "--force-in-use", f)[0], 0)
            finally:
                stop(child)

    def test_directory_a_process_works_in_is_in_use(self):
        with Workspace() as w:
            d = w.dev / ".tmp" / "server"
            d.mkdir()
            child = holding(None, cwd=str(d))
            try:
                rc, _, err = dev("rm", "-r", d)
                self.assertEqual(rc, 1)
                self.assertIn("in use", err)
            finally:
                stop(child)
            self.assertEqual(dev("rm", "-r", d)[0], 0)

    def test_own_working_directory_cannot_be_deleted(self):
        with Workspace() as w:
            d = w.dev / ".tmp" / "here"
            d.mkdir()
            os.chdir(d)
            rc, _, err = dev("rm", "-r", "--force-in-use", ".")
            self.assertIn("your working directory is inside it", err)
            self.assertIn("no override", err)

    def test_batch_is_all_or_nothing(self):
        with Workspace() as w:
            a = w.write("a.log")
            rc, _, err = dev("rm", "a.log", "README.md")
            self.assertEqual(rc, 1)
            self.assertIn("refused 1 of 2 paths; nothing was deleted", err)
            self.assertTrue(a.exists())

    def test_dry_run_and_missing_paths(self):
        with Workspace() as w:
            w.write("b.log")
            rc, out, _ = dev("rm", "--dry-run", "b.log")
            self.assertEqual(rc, 0)
            self.assertIn("would trash 1 item", out)
            self.assertTrue((w.repo / "b.log").exists())
            self.assertEqual(dev("rm", "missing.txt")[0], 1)
            self.assertEqual(dev("rm", "-f", "missing.txt")[1].strip(), "nothing to delete")

    def test_temp_roots_skip_git_rules(self):
        with Workspace() as w:
            clone = Workspace.make_repo(w.dev / ".tmp" / "clone", {"a.txt": "a\n"})
            os.chdir(w.base)
            rc, out, err = dev("rm", "-r", clone)
            self.assertEqual(rc, 0, err)
            self.assertFalse(clone.exists())

    def test_list_and_undo_several_runs(self):
        with Workspace() as w:
            w.write("one.log")
            w.write("two.log")
            dev("rm", "one.log")
            dev("rm", "two.log")
            rc, out, _ = dev("rm", "--list")
            self.assertEqual(rc, 0)
            body = out.splitlines()[1:]
            self.assertEqual(len(body), 2)
            self.assertIn("two.log", body[0])  # newest first
            dev("rm", "--undo", "2")
            self.assertTrue((w.repo / "one.log").exists() and (w.repo / "two.log").exists())
            self.assertIn("restored", dev("rm", "--list")[1])

    def test_parse_trash_output(self):
        out = ('# Moved "/a/b c.txt" to "/Users/x/.Trash/b c.txt"\n'
               '# Moved "/a/d/b c.txt" to "/Users/x/.Trash/b c 20-36-56-266.txt"\n')
        self.assertEqual(fs._parse_trash(out, ["/a/b c.txt", "/a/d/b c.txt", "/a/gone"]),
                         {"/a/b c.txt": "/Users/x/.Trash/b c.txt",
                          "/a/d/b c.txt": "/Users/x/.Trash/b c 20-36-56-266.txt"})


class MvCpTests(unittest.TestCase):
    def test_mv_never_overwrites_without_force(self):
        with Workspace() as w:
            w.write("a.txt", "new\n")
            w.write("b.txt", "old\n")
            rc, _, err = dev("mv", "a.txt", "b.txt")
            self.assertEqual(rc, 1)
            self.assertIn("would overwrite b.txt (4 B, changed", err)
            self.assertIn("retry with --force", err)
            rc, out, _ = dev("mv", "--force", "a.txt", "b.txt")
            self.assertEqual(rc, 0)
            self.assertIn("replaced 1", out)
            self.assertEqual((w.repo / "b.txt").read_text(), "new\n")
            self.assertEqual(len(w.trashed), 1)  # the old b.txt is recoverable

    def test_mv_tracked_uses_git_mv(self):
        with Workspace() as w:
            rc, out, _ = dev("mv", "src/util.py", "src/helpers.py")
            self.assertEqual(rc, 0)
            self.assertIn("git mv", out)
            self.assertIn("R  src/util.py -> src/helpers.py", git(w.repo, "status", "--porcelain"))

    def test_mv_across_repositories_needs_cross(self):
        with Workspace() as w:
            other = Workspace.make_repo(w.dev / "acme" / "other", {"a.txt": "a\n"})
            w.write("move-me.txt")
            rc, _, err = dev("mv", "move-me.txt", other)
            self.assertIn("across projects", err)
            self.assertIn("retry with --cross", err)
            self.assertEqual(dev("mv", "--cross", "move-me.txt", other)[0], 0)
            self.assertTrue((other / "move-me.txt").exists())

    def test_mv_of_a_repository_points_to_adopt(self):
        with Workspace() as w:
            os.chdir(w.base)
            rc, _, err = dev("mv", w.repo, w.dev / "acme" / "renamed")
            self.assertIn("dev adopt", err)

    def test_layout_violations_are_refused(self):
        with Workspace() as w:
            for dest in ("src/app.py.bak", "src/app_old.py", "src/app copy.py", "src/app.py.orig"):
                with self.subTest(dest=dest):
                    rc, _, err = dev("cp", "src/app.py", dest)
                    self.assertEqual(rc, 1)
                    self.assertIn("backup-style name", err)
            w.write("pkg/a.txt")
            for dest in (w.dev / "acme" / "widget-pr12", w.dev / "acme" / "widget-copy", w.dev / "loose-zz"):
                with self.subTest(dest=dest):
                    rc, _, err = dev("cp", "-r", "pkg", dest)
                    self.assertEqual(rc, 1)
                    self.assertFalse(dest.exists())
            rc, _, err = dev("cp", "-r", "--include-heavy", w.repo, w.dev / "acme" / "spare")
            self.assertIn("sibling copy", err)

    def test_cp_dir_needs_r_and_refuses_heavy_dirs(self):
        with Workspace() as w:
            w.write("pkg/index.js", "x\n")
            w.write("pkg/node_modules/dep/i.js", "y" * 4096)
            dest = w.dev / ".tmp" / "pkg-copy"
            self.assertIn("retry with -r", dev("cp", "pkg", dest)[2])
            rc, _, err = dev("cp", "-r", "pkg", dest)
            self.assertIn("contains pkg/node_modules/", err)
            self.assertIn("--skip-heavy", err)
            rc, out, _ = dev("cp", "-r", "--skip-heavy", "pkg", dest)
            self.assertEqual(rc, 0)
            self.assertIn("1 file, 2 B", out)
            self.assertIn("skipped pkg/node_modules/", out)
            self.assertFalse((dest / "node_modules").exists())

    def test_cp_merges_into_existing_dir_but_never_clobbers(self):
        with Workspace() as w:
            w.write("pkg/a.txt", "a\n")
            w.write("pkg/b.txt", "b\n")
            w.write("dest/pkg/b.txt", "keep\n")
            rc, _, err = dev("cp", "-r", "pkg", "dest/")
            self.assertIn("would overwrite dest/pkg/b.txt", err)
            self.assertEqual((w.repo / "dest/pkg/b.txt").read_text(), "keep\n")
            w.write("other/x.txt")
            self.assertEqual(dev("cp", "-r", "other", "dest/")[0], 0)
            self.assertTrue((w.repo / "dest/other/x.txt").exists())


class FilesTests(unittest.TestCase):
    def test_tree_dirs_first_markers_and_ignored(self):
        with Workspace() as w:
            w.write("node_modules/x/i.js")
            w.write("src/app.py", "changed\n")
            w.write("src/new.py")
            rc, out, _ = dev("files")
            lines = out.splitlines()
            self.assertEqual(rc, 0)
            self.assertRegex(lines[0], r"^\./  6 files, .*git: \?\? 1, M 1$")
            self.assertEqual(lines[1], "ignored: node_modules/ (dev files --all)")
            self.assertEqual(lines[2], "src/  (3 files)  [?? 1, M 1]")
            self.assertIn("  app.py  M", lines)
            self.assertIn("  new.py  ??", lines)
            self.assertLess(lines.index("src/  (3 files)  [?? 1, M 1]"), lines.index("README.md"))
            self.assertNotIn("i.js", out)

    def test_many_files_of_one_extension_fold_into_one_line(self):
        with Workspace() as w:
            for i in range(40):
                w.write(f"assets/img{i:02d}.png", "p" * 100)
            git(w.repo, "add", "-A")
            git(w.repo, "commit", "-q", "-m", "assets")
            w.write("assets/img07.png", "changed")
            w.write("assets/notes.txt")
            out = dev("files", "assets")[1].splitlines()
            self.assertIn("*.png ×39  (3 KB)", out)
            self.assertIn("img07.png  M", out)
            self.assertIn("notes.txt  ??", out)

    def test_output_is_capped_with_a_more_line(self):
        with Workspace() as w:
            for i in range(30):
                w.write(f"d{i:02d}/a.txt")
                w.write(f"d{i:02d}/b.txt")
            rc, out, _ = dev("files", "--max", "20")
            lines = out.splitlines()
            self.assertLessEqual(len(lines), 20)
            self.assertRegex(lines[-1], r"^… \+\d+ more — narrow with dev files <subdir>")

    def test_json_and_non_repo_walk(self):
        with Workspace() as w:
            data = json.loads(dev("files", "--json")[1])
            self.assertTrue(data["repo"])
            self.assertIn({"type": "dir", "files": 2, "bytes": data["entries"][0]["bytes"], "git": {},
                           "repo": False, "level": 1, "path": "src/"}, data["entries"])
            plain = w.dev / ".tmp" / "plain"
            w.write("node_modules/x.js", root=plain)
            w.write("main.c", root=plain)
            out = dev("files", plain)[1]
            self.assertIn("skipped: node_modules/", out)
            self.assertIn("main.c", out)


class FindTests(unittest.TestCase):
    def test_glob_ext_type_and_ignore(self):
        with Workspace() as w:
            w.write("node_modules/x/app.py")
            self.assertEqual(dev("find", "*.py")[1].split(), ["src/app.py", "src/util.py"])
            self.assertEqual(dev("find", "-e", "py")[1].split(), ["src/app.py", "src/util.py"])
            self.assertEqual(dev("find", "-t", "d")[1].split(), ["src/"])
            self.assertIn("node_modules/x/app.py", dev("find", "--all", "app")[1].split())
            self.assertEqual(dev("find", "nothing-like-this")[0], 1)

    def test_changed_since_size_and_relative_paths(self):
        with Workspace() as w:
            w.write("src/util.py", "VALUE = 9\n")
            w.write("src/fresh.py")
            self.assertEqual(dev("find", "--changed")[1].split(), ["src/fresh.py", "src/util.py"])
            self.assertEqual(dev("find", "--changed", "-t", "d")[1].split(), ["src/"])
            old = time.time() - 5 * 86400
            for f in (w.repo / "README.md", w.repo / ".gitignore", w.repo / "package-lock.json"):
                os.utime(f, (old, old))
            self.assertNotIn("README.md", dev("find", "--since", "1d")[1])
            self.assertEqual(dev("find", "--size", "+1k")[1].split(), ["package-lock.json"])
            os.chdir(w.repo / "src")
            self.assertEqual(dev("find", "util")[1].split(), ["util.py"])

    def test_cap(self):
        with Workspace() as w:
            for i in range(10):
                w.write(f"many/f{i}.txt")
            out = dev("find", "-e", "txt", "--max", "3")[1].splitlines()
            self.assertEqual(len(out), 4)
            self.assertTrue(out[-1].startswith("+7 more — narrow with"))

    def test_matcher_heuristics(self):
        self.assertTrue(fs._matcher("*.py")("app.py"))
        self.assertFalse(fs._matcher("*.py")("app.pyc"))
        self.assertTrue(fs._matcher("test_.*fs")("test_my_fs.py"))  # .* makes it a regex
        self.assertTrue(fs._matcher("readme")("README.md"))         # smart case
        self.assertFalse(fs._matcher("Readme")("README.md"))


class GrepTests(unittest.TestCase):
    def check_basic(self, w):
        w.write("src/long.py", "x = 1\n" + "a" * 300 + " NEEDLE " + "b" * 300 + "\n")
        w.write("src/many.py", "".join(f"hit_{i} = {i}\n" for i in range(20)))
        rc, out, _ = dev("grep", "return 3")
        self.assertEqual((rc, out.strip()), (0, "src/app.py:8: return 3"))
        out = dev("grep", "NEEDLE")[1].strip()
        self.assertTrue(out.startswith("src/long.py:2: …"), out)
        self.assertIn("NEEDLE", out)
        self.assertLessEqual(len(out) - len("src/long.py:2: "), fs.LINE_WIDTH + 2)
        lines = dev("grep", "hit_")[1].splitlines()
        self.assertEqual(lines[:2], ["src/many.py:1: hit_0 = 0", "src/many.py:2: hit_1 = 1"])
        self.assertEqual(lines[5], "src/many.py: +15 more")
        self.assertEqual(dev("grep", "-l", "def|hit_")[1].split(), ["src/app.py", "src/many.py"])
        self.assertIn("src/many.py: 20", dev("grep", "--count", "hit_")[1])
        data = json.loads(dev("grep", "--json", "hit_")[1])
        self.assertEqual((data["total"], len(data["matches"])), (20, 5))
        self.assertEqual(dev("grep", "zzz-no-such")[0], 1)
        out = dev("grep", "-C", "1", "return 2")[1].splitlines()
        self.assertEqual(out, ["src/app.py-5- def f2():", "src/app.py:6: return 2", "src/app.py-7- def f3():"])

    @unittest.skipUnless(core.have("rg"), "ripgrep not installed")
    def test_ripgrep(self):
        with Workspace() as w:
            self.check_basic(w)

    def test_python_fallback(self):
        have = core.have
        core.have = lambda tool: tool != "rg" and have(tool)
        try:
            with Workspace() as w:
                self.check_basic(w)
        finally:
            core.have = have

    def test_total_cap(self):
        with Workspace() as w:
            for i in range(30):
                w.write(f"m/f{i:02d}.txt", "token\n")
            lines = dev("grep", "token", "--max", "10")[1].splitlines()
            self.assertEqual(len(lines), 11)
            self.assertEqual(lines[-1], "+20 more matching lines in 20 files — narrow with a path, -g '*.ext', "
                                        "-t TYPE, or list files with -l")


class DiffTests(unittest.TestCase):
    def test_stat_first_then_short_hunks_and_folded_lock_file(self):
        with Workspace() as w:
            w.write("src/app.py", (w.repo / "src/app.py").read_text().replace("return 2", "return 22"))
            w.write("package-lock.json", "{}\n")
            w.write("untracked.txt")
            out = dev("diff")[1].splitlines()
            self.assertEqual(out[0], "2 files changed, +2 -51")
            self.assertEqual(out[1], "M package-lock.json  +1 -50  (folded: lock file — dev diff package-lock.json)")
            self.assertEqual(out[2], "M src/app.py  +1 -1")
            self.assertTrue(out[3].startswith("untracked (not in this diff): untracked.txt"))
            self.assertEqual(out[4:], ["── src/app.py", "@@ -5,3 +5,3 @@ def f1():", " def f2():",
                                       "-    return 2", "+    return 22", " def f3():"])
            explicit = dev("diff", "package-lock.json")[1]
            self.assertIn("── package-lock.json", explicit)

    def test_cap_names_staged_and_word(self):
        with Workspace() as w:
            w.write("src/app.py", "".join(f"line {i}\n" for i in range(100)))
            out = dev("diff", "--max", "12")[1].splitlines()
            self.assertEqual(len(out), 12)
            self.assertRegex(out[-1], r"^\+\d+ lines — dev diff src/app.py --max 0$")
            self.assertEqual(dev("diff", "--names")[1].strip(), "M src/app.py")
            self.assertIn("no changes", dev("diff", "--staged")[1])
            git(w.repo, "add", "src/app.py")
            self.assertIn("· staged", dev("diff", "--staged")[1])
            w.write("README.md", "# widget tool\n")
            self.assertIn("# widget {+tool+}", dev("diff", "--word", "README.md")[1])

    def test_two_files_outside_git(self):
        with Workspace() as w:
            a = w.write("a.txt", "one\ntwo\n", root=w.outside)
            b = w.write("b.txt", "one\nthree\n", root=w.outside)
            os.chdir(w.outside)
            out = dev("diff", "a.txt", "b.txt")[1].splitlines()
            self.assertEqual(out[0], "1 file changed, +1 -1")
            self.assertEqual(out[1], "a.txt → b.txt  +1 -1")
            self.assertIn("-two", out)
            self.assertIn("+three", out)
            self.assertTrue(a.exists() and b.exists())


class OutputTests(unittest.TestCase):
    def test_no_colour_when_not_a_tty(self):
        with Workspace() as w:
            w.write("src/app.py", "changed\n")
            for argv in (("files",), ("find", "py"), ("grep", "VALUE"), ("diff",), ("rm", "README.md")):
                with self.subTest(argv=argv):
                    a = parser().parse_args(list(argv))
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        a.fn(a)
                    self.assertNotIn("\x1b[", out.getvalue())

    def test_registered_names_do_not_clash_with_existing_commands(self):
        names = set(parser()._subparsers._group_actions[0].choices)
        self.assertEqual(names, {"rm", "mv", "cp", "files", "find", "grep", "diff"})
        self.assertFalse(names & {"ls", "du", "status", "go"})
        self.assertTrue(all(re.match(r"^[a-z]+$", n) for n in names))


if __name__ == "__main__":
    unittest.main()
