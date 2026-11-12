# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Disk use on this Mac and the plan to free it (spec P-fxs316lh3nw R4-R8, Sati's `dev du` design and Hamann's
# decision (b): `dev free`, with `dev gc` as its old name). Every test builds its own git repositories, home folder,
# state folder and fake `docker`, `colima`, `brew`, `npm`, `npx`, `uv` and `pnpm` in a temp directory, first on the
# PATH, so no test reads the developer's projects, config or Docker, and nothing real is ever deleted. Thresholds are
# lowered to kilobytes so small fixture files stand for gigabytes. Tests carry comments, not docstrings.
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, cli, core, disk, gc, registry, ui  # noqa: E402

ISOLATE = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}
GB = 1024 * 1024  # KB in a GB, for reports written by hand


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


def write(path: Path, kb: int):
    # real bytes, so du counts them
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(kb * 1024))


DOCKER = """#!/bin/sh
echo "docker $*" >> "{log}"
case "$*" in
  "info --format {{{{.ServerVersion}}}}") echo 27.3.1 ;;
  "system df --format {{{{json .}}}}") cat "{dir}/df.jsonl" ;;
  "image ls --format {{{{json .}}}}") cat "{dir}/images.jsonl" ;;
  "ps -a --format {{{{.Image}}}}") cat "{dir}/ps.txt" ;;
  "image prune -a -f") echo "Total reclaimed space: 30MB" ;;
  *) exit 0 ;;
esac
"""
COLIMA = """#!/bin/sh
echo "colima $*" >> "{log}"
case "$*" in
  "list --json") cat "{dir}/colima.jsonl" ;;
  *"-- df -k /var/lib/docker /")
    echo "Filesystem 1K-blocks Used Available Use% Mounted on"
    echo "/dev/vdb1 81985944 2048 75359216 4% /var/lib/docker"
    echo "/dev/root 19221248 1024 18320236 5% /" ;;
  *"-- sudo fstrim -av") echo "/var/lib/docker: 15 GiB trimmed" ;;
  *) exit 0 ;;
esac
"""
TOOL = """#!/bin/sh
echo "{name} $*" >> "{log}"
"""


class Sandbox(unittest.TestCase):
    # A temp home, state folder, handoff log and fake tools; git with no global or system config.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(core.real(self.tmp.name))
        # the temp root is the home folder, with ~/Development in it, as on a Mac
        self.home, self.state, self.bin = self.root, self.root / "state", self.root / "bin"
        for d in (self.state, self.bin):
            d.mkdir()
        self.log = self.root / "calls.log"
        self.log.write_text("")
        self.audited, self.cwds, self.procs = [], [], []
        for name in ("brew", "npm", "npx", "uv", "pnpm"):
            self.tool(name, TOOL.format(name=name, log=self.log))
        self.tool("docker", "#!/bin/sh\nexit 1\n")  # not installed or not running, unless a test fakes it
        self.tool("colima", "#!/bin/sh\nexit 1\n")
        path = f"{self.bin}:{os.environ.get('PATH', '')}"
        env = dict(ISOLATE, PATH=path, HOME=str(self.home))
        patches = [mock.patch.object(core, "STATE", self.state), mock.patch.object(core, "HOME", self.home),
                   mock.patch.object(core, "HANDOFF", self.root / "handoff"),
                   mock.patch.object(core, "CLAUDE_HOME", self.home / ".claude"),
                   mock.patch.object(core, "CODEX_HOME", self.home / ".codex"),
                   mock.patch.object(core, "TMP_ROOT", self.root / "dev-tmp"),
                   mock.patch.dict(core.GIT_ENV, dict(ISOLATE, PATH=path)),
                   mock.patch.dict(os.environ, env),
                   mock.patch.object(gc, "LOGDIR", self.root / "handoff-log"),
                   mock.patch.object(audit, "record", lambda *a, **k: self.audited.append(a)),
                   mock.patch.object(core, "active_cwds", lambda: self.cwds),
                   mock.patch.object(disk, "_processes", lambda: self.procs),
                   mock.patch.object(disk, "MIN_FOLDER_KB", 8), mock.patch.object(disk, "MIN_ITEM_KB", 16),
                   mock.patch.object(disk, "SMALL_PROJECT_KB", 1),
                   mock.patch.object(disk, "_disk_usage", lambda: (995 * GB, 798 * GB))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in ("HOMEBREW_CACHE", "PLAYWRIGHT_BROWSERS_PATH", "UV_CACHE_DIR", "PNPM_STORE_DIR", "COLIMA_HOME",
                  "CLAUDE_CODE_TMPDIR"):  # the developer's own cache locations never reach a test
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, name, script):
        f = self.bin / name
        f.write_text(script)
        f.chmod(0o755)

    def calls(self):
        return self.log.read_text().splitlines()

    def fake_docker(self, df, images, ps=""):
        (self.root / "df.jsonl").write_text("\n".join(json.dumps(x) for x in df) + "\n")
        (self.root / "images.jsonl").write_text("\n".join(json.dumps(x) for x in images) + "\n")
        (self.root / "ps.txt").write_text(ps)
        self.tool("docker", DOCKER.format(log=self.log, dir=self.root))

    def fake_colima(self, status="Running", on_mac_kb=64):
        (self.root / "colima.jsonl").write_text(json.dumps(
            {"name": "default", "status": status, "disk": 85899345920, "runtime": "docker"}) + "\n")
        write(self.home / ".colima" / "_lima" / "_disks" / "colima" / "datadisk", on_mac_kb)
        self.tool("colima", COLIMA.format(log=self.log, dir=self.root))

    def repo(self, name="kit-consulting"):
        # a checkout whose origin is a bare repository, with one pushed commit and an ignore file
        origin = self.root / "remotes" / f"{name}.git"
        origin.parent.mkdir(exist_ok=True)
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        path = self.root / "Development" / name
        path.parent.mkdir(exist_ok=True)
        git(self.root, "clone", "-q", str(origin), str(path))
        git(path, "checkout", "-q", "-b", "main")
        (path / ".gitignore").write_text("dist/\nnode_modules/\n.pytest_cache/\nmodels/\nenv311/\n")
        (path / "README.md").write_text("kit\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
        git(path, "push", "-q", "origin", "main")
        git(path, "remote", "set-head", "origin", "main")
        return path

    def worktree(self, main, path, branch):
        git(main, "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
        git(path, "push", "-q", "-u", "origin", branch)
        return path

    def project(self, pid, path, worktrees=(), canonical_ok=True, remote="git@github.com:acme/x.git", github=None,
                **main):
        owner, _, name = pid.rpartition("/")
        co = dict({"branch": "main", "path": str(path), "modified": 0, "untracked": 0, "unpushed": 0,
                   "ahead_default": 0, "active": False, "last_activity": int(time.time()) - 600}, **main)
        return {"id": pid, "owner": owner, "name": name, "path": str(path), "local": True, "main": co,
                "worktrees": list(worktrees), "canonical_ok": canonical_ok, "canonical": str(path),
                "remote": remote, "github": github, "default_branch": "main"}

    def wt(self, path, location, branch=None, **kw):
        return dict({"path": str(path), "branch": branch, "location": location, "modified": 0, "untracked": 0,
                     "unpushed": 0, "ahead_default": 0, "active": False, "locked": False, "pr": None,
                     "last_activity": int(time.time()) - 3600}, **kw)

    def measure(self, *projects):
        return disk.measure({"projects": list(projects)}, home=self.home, tmp=self.root / "tmp")

    def text(self, report, cols=80):
        # the screen as running text: wrapped lines joined, so a sentence can be found wherever it wraps
        return " ".join(" ".join(disk.render(report, cols=cols)).split())


def item(report, kind):
    return next(i for i in report["plan"] if i["kind"] == kind)


def run(fn, ns):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(ns)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- R4: what each project holds


class MeasuringTests(Sandbox):
    # The largest folders come from measuring what git ignores, at any depth, and are named by what they are: a Rust
    # target three folders down, a virtual environment with an unusual name (found by its pyvenv.cfg), a cache, and
    # an ignored folder whose name devkit does not know. Tracked files are never listed.
    def test_largest_folders_come_from_measuring_what_git_ignores(self):
        r = self.repo()
        write(r / "dist" / "app.js", 20)
        (r / "app" / "src-tauri").mkdir(parents=True)
        (r / "app" / "src-tauri" / "Cargo.toml").write_text("[package]\n")
        (r / "app" / "src-tauri" / ".gitignore").write_text("target/\n")
        write(r / "app" / "src-tauri" / "target" / "debug" / "app", 40)
        write(r / "env311" / "lib" / "site.py", 16)
        (r / "env311" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        write(r / ".pytest_cache" / "v" / "cache", 12)
        write(r / "models" / "weights.bin", 30)
        write(r / "src" / "big.py", 40)
        found = {f["rel"]: f for f in disk.ignored_folders(str(r))}
        self.assertEqual(found["app/src-tauri/target"]["kind"], "build")
        self.assertTrue(found["app/src-tauri/target"].get("rust"))
        self.assertEqual(found["dist"]["kind"], "build")
        self.assertEqual(found["env311"]["kind"], "deps")
        self.assertEqual(found[".pytest_cache"]["kind"], "cache")
        self.assertEqual(found["models"]["kind"], "other")
        self.assertNotIn("src", found)
        sizes = [f["kb"] for f in disk.ignored_folders(str(r))]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    # Each project on this Mac shows its main checkout, managed worktrees, Claude Code and Codex worktrees, and copies
    # outside the layout (flagged); a project that is only in the portfolio is not measured (R1).
    def test_rows_name_each_kind_of_checkout_and_flag_copies(self):
        r = self.repo()
        dev = r.parent
        wts = [self.wt(dev / ".worktrees" / "feat-a", "canonical", "feat/a", ahead_default=3),
               self.wt(r / ".claude" / "worktrees" / "macos-app-6b4769", "claude"),
               self.wt(self.home / ".codex" / "worktrees" / "c1", "codex", "codex/c1"),
               self.wt(dev / "kit-consulting-ci", "stray", "fix/chat", ahead_default=9, modified=4)]
        for w in wts:
            write(Path(w["path"]) / "file", 20)
        away = {"id": "fireflyframework/flyquery", "name": "flyquery", "path": None, "local": False,
                "main": {}, "worktrees": []}
        rep = self.measure(self.project("acme/kit-consulting", r, wts, github="acme/kit-consulting"), away)
        self.assertEqual([p["alias"] for p in rep["projects"]], ["kit-consulting"])
        self.assertEqual(rep["portfolio_only"], 1)
        kinds = [c["kind"] for c in rep["projects"][0]["checkouts"]]
        self.assertEqual(kinds, ["main", "worktree", "claude", "codex", "copy"])
        text = self.text(rep)
        self.assertIn("Claude Code worktrees", text)
        self.assertIn("Codex worktrees", text)
        self.assertIn("managed by the Codex app", text)
        self.assertIn("! Copies outside the layout", text)
        self.assertIn("fix/chat, no PR, 4 changed files", text)
        self.assertNotIn("flyquery", text)

    # A project whose worktrees and copies are more than twice its main checkout says why, and a project outside the
    # layout gets the command that fixes it.
    def test_why_so_big_names_the_layout_fix(self):
        r = self.repo()
        copy = self.wt(r.parent / "kit-consulting-ci", "stray", "fix/chat", ahead_default=9)
        write(Path(copy["path"]) / "dist" / "big.js", 600)  # more than twice the main checkout and its .git
        rep = self.measure(self.project("acme/kit-consulting", r, [copy], canonical_ok=False))
        text = self.text(rep)
        self.assertIn("Why so big: each worktree and copy is a full checkout", text)
        self.assertIn("dev adopt ~/Development/kit-consulting", text)
        small = self.measure(self.project("acme/kit-consulting", r))
        self.assertNotIn("Why so big", self.text(small))


# --------------------------------------------------------------------------- R5: Colima and Docker


class ColimaTests(Sandbox):
    def docker_df(self, volumes=0):
        return [{"Type": "Images", "TotalCount": "3", "Active": "1", "Size": "4.5MB", "Reclaimable": "3MB (66%)"},
                {"Type": "Containers", "TotalCount": "1", "Active": "0", "Size": "0B", "Reclaimable": "0B"},
                {"Type": "Local Volumes", "TotalCount": str(volumes), "Active": "0",
                 "Size": "5GB" if volumes else "0B", "Reclaimable": "5GB" if volumes else "0B"},
                {"Type": "Build Cache", "TotalCount": "0", "Active": "0", "Size": "0B", "Reclaimable": "0B"}]

    def images(self):
        return [{"Repository": "semgrep/semgrep", "Tag": "<none>", "ID": "4a34048c04fe", "Size": "2MB"},
                {"Repository": "ghcr.io/gitleaks/gitleaks", "Tag": "<none>", "ID": "c00b6bd0aeb3", "Size": "1MB"},
                {"Repository": "postgres", "Tag": "16", "ID": "aa11bb22cc33", "Size": "1.5MB"}]

    # Colima's disk takes far more on the Mac than Docker holds in it: dev du says how big the file can grow, what it
    # takes today, what Docker holds, and that the rest is free space inside the virtual machine that macOS has not
    # got back; returning it is plan item 1 and deletes nothing.
    def test_colima_explains_the_space_macos_has_not_got_back(self):
        self.fake_colima(on_mac_kb=8192)  # 8 MB on the Mac, 3 MB used inside: 5 MB to return, more than the images
        self.fake_docker(self.docker_df(), self.images(), ps="postgres:16\n")
        rep = self.measure()
        c = rep["colima"]
        self.assertEqual(c["vm_used_kb"], 3072)  # what df reports inside the virtual machine
        self.assertGreater(c["returnable_kb"], 50)
        trim = item(rep, "trim")
        self.assertEqual(trim["n"], 1)
        self.assertEqual(trim["runs"], "dev docker trim")
        text = self.text(rep)
        self.assertIn("Its disk is one file that can grow to 80 GB", text)
        self.assertIn("which macOS has not got back", text)
        self.assertIn("Returning it deletes nothing (plan item 1)", text)

    # Unused images are one plan item that runs `docker image prune -a -f` and then the trim; an image a container
    # uses is not counted, and volumes are explained but never in the plan.
    def test_unused_images_are_one_item_and_volumes_never_are(self):
        self.fake_colima(on_mac_kb=8)
        self.fake_docker(self.docker_df(volumes=2), self.images(), ps="postgres:16\n")
        rep = self.measure()
        img = item(rep, "images")
        self.assertEqual(img["kb"], disk.docker_kb("2MB") + disk.docker_kb("1MB"))
        self.assertEqual(img["runs"], "docker image prune -a -f, then the trim")
        self.assertFalse([i for i in rep["plan"] if "volume" in json.dumps(i).lower()])
        self.assertIn("devkit never deletes them", self.text(rep))

    # `dev docker trim` runs fstrim inside the virtual machine and reports what macOS got back.
    def test_docker_trim_runs_fstrim_inside_the_vm(self):
        self.fake_colima()
        rc, out, _ = run(cli.cmd_docker, argparse.Namespace(action="trim"))
        self.assertEqual(rc, 0)
        self.assertIn("colima ssh -p default -- sudo fstrim -av", self.calls())
        self.assertIn("macOS", out)

    # With Colima stopped, the trim says how to start it instead of failing silently.
    def test_trim_with_colima_stopped_says_how_to_start_it(self):
        self.fake_colima(status="Stopped")
        rc, out, err = run(cli.cmd_docker, argparse.Namespace(action="trim"))
        self.assertEqual(rc, 1)
        self.assertIn("dev docker up", out + err)
        self.assertFalse([c for c in self.calls() if "fstrim" in c])


# --------------------------------------------------------------------------- R7: agent data and caches


class AgentDataTests(Sandbox):
    # Each kind of agent data says what it is and whether it is safe to clear; only the safe ones with a command of
    # their own tool reach the plan, and chats are kept.
    def test_agent_data_says_what_each_is_and_whether_it_is_safe(self):
        write(self.home / "Library" / "Caches" / "Homebrew" / "downloads" / "x.tar.gz", 40)
        write(self.home / ".claude" / "projects" / "-Users-ana-kit" / "s.jsonl", 30)
        write(self.home / ".codex" / "sessions" / "2026" / "s.jsonl", 20)
        write(self.home / "Library" / "Caches" / "ms-playwright" / "chromium" / "chrome", 24)
        write(self.home / ".cache" / "uv" / "wheels" / "w", 4)
        rows = {r["key"]: r for r in disk.agent_data(home=self.home, tmp=self.root / "tmp")}
        self.assertEqual(rows["brew"]["safety"], "safe")
        self.assertEqual(rows["claude-chats"]["safety"], "keep")
        self.assertEqual(rows["codex-chats"]["safety"], "keep")
        rep = self.measure()
        cmds = {i["key"]: i["runs"] for i in rep["plan"]}
        self.assertEqual(cmds["cache:brew"], "brew cleanup --prune=all")
        self.assertEqual(cmds["cache:playwright"], "npx --yes playwright uninstall --all")
        self.assertNotIn("cache:uv", cmds)  # under the plan's minimum
        self.assertFalse([k for k in cmds if "chats" in k])
        text = self.text(rep)
        self.assertIn("keep: your chat history, used to resume", text)
        self.assertIn("safe: downloaded again when needed", text)


# --------------------------------------------------------------------------- R6: the plan


class PlanTests(Sandbox):
    # The plan is ranked by the space each item frees, and each item has a safety word, one sentence on what
    # happens afterwards, and its command; items under the minimum are left out.
    def test_the_plan_is_ranked_with_safety_risk_and_command(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-ci", "fix/chat")
        write(copy / "dist" / "big.js", 60)
        cl = r / ".claude" / "worktrees" / "macos-app-6b4769"
        write(cl / "f", 30)
        write(self.home / "Library" / "Caches" / "Homebrew" / "x", 20)
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "fix/chat", ahead_default=2, pr={"number": 7, "state": "OPEN"}),
            self.wt(cl, "claude")]))
        plan = rep["plan"]
        self.assertEqual([i["kb"] for i in plan], sorted((i["kb"] for i in plan), reverse=True))
        self.assertEqual([i["n"] for i in plan], list(range(1, len(plan) + 1)))
        self.assertEqual({i["kind"] for i in plan}, {"build", "worktrees", "cache"})
        for i in plan:
            self.assertIn(i["safety"], ("safe", "check first"))
            self.assertTrue(i["risk"])
        build = item(rep, "build")
        self.assertEqual(build["what"], "Build output in the kit-consulting-ci copy")
        text = self.text(rep)
        self.assertIn(f"dev free {build['n']}", text)
        self.assertIn("Nothing has been deleted.", text)

    # A merged Claude Code worktree with commits that are not pushed is `check first`, and says how to fix it.
    def test_a_worktree_with_commits_not_pushed_is_check_first(self):
        r = self.repo()
        cl = r / ".claude" / "worktrees" / "macos-app-6b4769"
        write(cl / "f", 30)
        rep = self.measure(self.project("acme/kit-consulting", r, [self.wt(cl, "claude", unpushed=2)]))
        wt = item(rep, "worktrees")
        self.assertEqual(wt["safety"], "check first")
        self.assertIn("macos-app-6b4769 has 2 commits that are not pushed. Push them first.", wt["risk"])

    # A copy whose branch main took by content (cherry-picked, or rebased by its pull request) is offered like a
    # merged one: the registry's merge test says so, and its commits on no remote are in main, not work to push.
    def test_a_copy_merged_by_content_is_offered_like_a_merged_one(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-picked", "fix/picked")  # git reads it, as gc.remove_worktree
        write(copy / "node_modules" / "f", 30)
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "fix/picked", ahead_default=2, unpushed=2, landed=2, merged="content")]))
        offered = [i for i in rep["plan"] if i["kind"] == "copy"]
        self.assertEqual([(i["safety"], i["risk"]) for i in offered],
                         [("safe", "Its commits have an equivalent in main (merged by content).")])

    # A main checkout in use keeps its build output out of the plan, and Codex worktrees are never removed (the
    # Codex app manages them).
    def test_main_in_use_and_codex_worktrees_stay_out_of_the_plan(self):
        r = self.repo()
        write(r / "dist" / "big.js", 60)
        cx = self.home / ".codex" / "worktrees" / "c1"
        write(cx / "f", 60)
        rep = self.measure(self.project("acme/kit-consulting", r, [self.wt(cx, "codex", "codex/c1")], active=True))
        self.assertEqual(rep["plan"], [])
        self.assertIn("Nothing is safe to free right now", self.text(rep))


# --------------------------------------------------------------------------- R8: the cache, --refresh and --json


def du_ns(**kw):
    return argparse.Namespace(**dict({"query": None, "refresh": False, "plan": False, "json": False}, **kw))


class CacheTests(Sandbox):
    def saved(self, ago=180):
        rep = self.measure()
        rep["measured"] = time.time() - ago
        disk.save(rep)
        return rep

    # `dev du` answers from the cache with its age and measures nothing; --refresh measures again.
    def test_du_answers_from_the_cache_and_refresh_measures_again(self):
        self.saved()
        with mock.patch.object(disk, "measure_now", side_effect=AssertionError("measured")):
            started = time.time()
            rc, out, _ = run(disk.cmd_du, du_ns())
            self.assertLess(time.time() - started, 2)
        self.assertEqual(rc, 0)
        self.assertIn("Disk use on this Mac · measured 3 min ago", out)
        fresh = self.measure()
        with mock.patch.object(disk, "measure_now", return_value=fresh) as m:
            run(disk.cmd_du, du_ns(refresh=True))
        m.assert_called_once()

    # --json gives the dashboard the measurement time and the numbered plan, with full values.
    def test_du_json_has_the_time_and_the_plan(self):
        write(self.home / "Library" / "Caches" / "Homebrew" / "x", 20)
        rep = self.saved()
        rc, out, _ = run(disk.cmd_du, du_ns(json=True))
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["measured"], rep["measured"])
        self.assertEqual(data["plan"][0]["n"], 1)
        self.assertEqual(data["plan"][0]["targets"][0]["path"], str(self.home / "Library" / "Caches" / "Homebrew"))

    # The screen fits 80 and 120 columns, its bars are 24 and 40 cells wide, and it has no color codes when color
    # is off (NO_COLOR or not a terminal).
    def test_du_fits_80_and_120_columns(self):
        r = self.repo()
        copy = self.wt(r.parent / "kit-consulting-ci-with-a-long-name", "stray",
                       "fix/a-branch-name-that-is-much-too-long-for-a-row", ahead_default=9, modified=4)
        write(Path(copy["path"]) / "dist" / "big.js", 60)
        self.fake_colima()
        rep = self.measure(self.project("acme/kit-consulting", r, [copy], canonical_ok=False))
        for cols, cells in ((80, 24), (120, 40)):
            lines = disk.render(rep, cols=cols)
            self.assertLessEqual(max(ui.visible_len(x) for x in lines), cols, cols)
            self.assertFalse([x for x in lines if "\x1b[" in x])
            bars = [x for x in lines if "█" in x]
            self.assertEqual(max(x.count("█") for x in bars), cells)


# --------------------------------------------------------------------------- dev free and dev gc (Hamann (b))


def free_ns(*items, safe=False, yes=False):
    return argparse.Namespace(items=[str(i) for i in items], safe=safe, yes=yes, apply=False, verbose=False,
                              auto_only=False)


class FreeTests(Sandbox):
    def setup_copy(self, changed=False):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-ci", "fix/chat")
        write(copy / "dist" / "big.js", 60)
        if changed:
            (copy / "README.md").write_text("changed\n")
        info = self.wt(copy, "stray", "fix/chat", ahead_default=2, modified=int(changed))
        rep = self.measure(self.project("acme/kit-consulting", r, [info]))
        disk.save(rep)
        return r, copy, rep

    def free(self, *items, answer="y", **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = disk.free([str(i) for i in items], ask=lambda prompt: answer, **kw)
        return rc, out.getvalue()

    # A number always means the item the developer saw: when a newer measurement changed the plan, dev free prints
    # the new plan and deletes nothing.
    def test_free_deletes_nothing_when_the_plan_changed_since_it_was_shown(self):
        r, copy, rep = self.setup_copy()
        disk.mark_seen(rep)
        write(self.home / "Library" / "Caches" / "Homebrew" / "x", 200)
        disk.save(self.measure(self.project("acme/kit-consulting", r, [self.wt(copy, "stray", "fix/chat",
                                                                                ahead_default=2)])))
        rc, out = self.free(1)
        self.assertEqual(rc, 1)
        self.assertIn("The plan changed since you saw it, so nothing was deleted", out)
        self.assertTrue((copy / "dist" / "big.js").exists())
        self.assertFalse([c for c in self.calls() if c.startswith("brew")])

    # After a yes, dev free deletes the build output (only what git ignores), keeps the changed files, reports what
    # it freed and records it in the audit trail.
    def test_free_deletes_build_output_and_keeps_changed_files(self):
        r, copy, rep = self.setup_copy(changed=True)
        disk.mark_seen(rep)
        n = item(rep, "build")["n"]
        rc, out = self.free(n)
        self.assertEqual(rc, 0, out)
        self.assertFalse((copy / "dist").exists())
        self.assertEqual((copy / "README.md").read_text(), "changed\n")
        self.assertIn("Freed", out)
        self.assertTrue([a for a in self.audited if a[0] == "ops" and "free" in " ".join(map(str, a))])
        self.assertIn(str(n), disk.load()["freed"])

    # A file git tracks inside ignored build output (added with git add -f after the measurement) is work, changed
    # or not: dev free keeps that whole folder and says why (Seraph's batch 4, follow-up 1). The check is git
    # ls-files itself, so it does not rest on how a git version's check-ignore treats such a folder.
    def test_free_keeps_build_output_that_holds_a_tracked_file(self):
        r, copy, rep = self.setup_copy()
        disk.mark_seen(rep)
        build = item(rep, "build")
        self.assertEqual(build["targets"][0]["folders"], ["dist"])
        (copy / "dist" / "keep.js").write_text("v1\n")
        git(copy, "add", "-f", "dist/keep.js")
        git(copy, "commit", "-q", "-m", "keep a built file")
        (copy / "dist" / "keep.js").write_text("v2, not committed\n")
        rc, out = self.free(build["n"])
        self.assertEqual(rc, 1, out)
        self.assertEqual((copy / "dist" / "keep.js").read_text(), "v2, not committed\n")
        self.assertTrue((copy / "dist" / "big.js").exists())
        self.assertIn("dist holds files git tracks", " ".join(out.split()))

    # Only build output a tool fills again (.next here) is safe; a generic output name (dist, build, target) can hold
    # files no build step wrote, so it is its own item, check first, with its folders and sizes, and dev free --safe
    # leaves it. A release worktree lost 3.6 GB of evidence under its ignored build/.
    def test_build_output_no_tool_fills_again_is_check_first_and_free_safe_leaves_it(self):
        r = self.repo()
        (r / ".git" / "info" / "exclude").write_text(".next/\n")
        copy = self.worktree(r, r.parent / "kit-consulting-ci", "fix/chat")
        asset = copy / "dist" / "a16-predecessor" / "asset.bin"
        write(asset, 60)
        write(copy / ".next" / "cache" / "x", 30)
        rep = self.measure(self.project("acme/kit-consulting", r,
                                        [self.wt(copy, "stray", "fix/chat", ahead_default=2)]))
        disk.save(rep)
        disk.mark_seen(rep)
        builds = sorted((i for i in rep["plan"] if i["kind"] == "build"), key=lambda i: i["safety"])
        self.assertEqual([(i["safety"], i["targets"][0]["folders"]) for i in builds],
                         [("check first", ["dist"]), ("safe", [".next"])])
        dist_kb = next(f["kb"] for f in rep["projects"][0]["checkouts"][1]["folders"] if f["rel"] == "dist")
        self.assertIn(f"dist ({ui.size_text(dist_kb)})", builds[0]["risk"])
        rc, out = self.free(safe_all=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(asset.exists(), out)
        self.assertFalse((copy / ".next").exists(), out)

    # dev free asks first: any answer but yes deletes nothing.
    def test_free_asks_first_and_no_deletes_nothing(self):
        r, copy, rep = self.setup_copy()
        disk.mark_seen(rep)
        prompts = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = disk.free([str(item(rep, "build")["n"])], ask=lambda p: prompts.append(p) or "n")
        self.assertEqual(rc, 1)
        self.assertIn("Delete it? [y/N]", prompts[0])
        self.assertTrue((copy / "dist" / "big.js").exists())

    # Safety is checked again at that moment: a copy that was safe to remove when measured but has a changed file
    # now is refused, with the reason and the fix, and stays on disk.
    def test_free_rechecks_safety_and_refuses_new_changes(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-pr12", "claude/vigilant")
        write(copy / "node_modules" / "x.js", 20)
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "claude/vigilant", pr={"number": 12, "state": "MERGED"})]))
        disk.save(rep)
        disk.mark_seen(rep)
        it = item(rep, "copy")
        self.assertEqual(it["safety"], "safe")
        (copy / "notes.txt").write_text("new work\n")
        rc, out = self.free(it["n"])
        self.assertEqual(rc, 1)
        self.assertIn("Not freed: kit-consulting-pr12 has 1 changed file. Commit or discard it first.", out)
        self.assertTrue(copy.exists())

    # A merged copy whose ignored files hold what no tool rebuilds (models/ here; a release worktree lost 3.6 GB of
    # evidence under its ignored build/) is not offered as safe: the plan names those files with their size, as
    # gc.remove_worktree would when it keeps the copy, and dev free --safe leaves it whole.
    def test_free_offers_a_merged_copy_with_ignored_files_no_tool_rebuilds_only_as_check_first(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-pr12", "claude/vigilant")
        write(copy / "models" / "weights.bin", 20)
        write(copy / "node_modules" / "x.js", 20)
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "claude/vigilant", pr={"number": 12, "state": "MERGED"})]))
        it = item(rep, "copy")
        self.assertEqual(it["safety"], "check first")
        self.assertIn("kit-consulting-pr12 stays: removing it would delete ignored files that are not known to be "
                      "regenerable: models/ (20 KB); move them out or delete them, then dev wt rm", it["risk"])
        self.assertNotIn("node_modules", it["risk"])

    # A merged, clean, pushed copy is removed with git worktree remove, so git forgets it too.
    def test_free_removes_a_merged_copy_with_git_worktree_remove(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-pr12", "claude/vigilant")
        write(copy / "node_modules" / "x.js", 20)
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "claude/vigilant", pr={"number": 12, "state": "MERGED"})]))
        disk.save(rep)
        disk.mark_seen(rep)
        rc, out = self.free(item(rep, "copy")["n"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(copy.exists())
        self.assertNotIn(str(copy), git(r, "worktree", "list"))

    # A copy merged by content goes although its commit is on no remote: the merge test runs again right before the
    # removal, and the local branch goes with it, since main has its commit.
    def test_free_removes_a_copy_merged_by_content_and_its_branch(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-picked", "fix/picked")
        (copy / "fix.txt").write_text("fix\n")
        git(copy, "add", "-A")
        git(copy, "commit", "-q", "-m", "fix: keep the picked thing")
        (r / "NEWS.md").write_text("news\n")  # main moves on, so the cherry-pick is a new commit
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "docs: add news")
        git(r, "cherry-pick", git(copy, "rev-parse", "HEAD"))
        git(r, "push", "-q", "origin", "main")
        rep = self.measure(self.project("acme/kit-consulting", r, [
            self.wt(copy, "stray", "fix/picked", ahead_default=1, unpushed=1, landed=1, merged="content")]))
        disk.save(rep)
        disk.mark_seen(rep)
        rc, out = self.free(item(rep, "copy")["n"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(copy.exists())
        self.assertEqual(git(r, "branch", "--list", "fix/picked"), "")

    # A copy whose branch GitHub squash-merged from two commits (and whose remote branch it deleted) is offered as
    # safe and goes: the registry's cached pull request says it is merged, and the merge test runs again with that
    # list right before the removal, so the plan and the removal agree. Its local branch goes with it.
    def test_free_removes_a_copy_squashed_by_its_own_pull_request_and_its_branch(self):
        r = self.repo()
        copy = self.worktree(r, r.parent / "kit-consulting-squashed", "feat/squashed")
        for k in (1, 2):
            (copy / f"part{k}.txt").write_text(f"part {k}\n")
            git(copy, "add", "-A")
            git(copy, "commit", "-q", "-m", f"feat: add part {k}")
        tip = git(copy, "rev-parse", "HEAD")
        git(r, "merge", "-q", "--squash", "feat/squashed")
        git(r, "commit", "-q", "-m", "feat: land feat/squashed (#7)")
        git(r, "push", "-q", "origin", "main", ":feat/squashed")
        pr = {"number": 7, "state": "MERGED", "headRefName": "feat/squashed", "headRefOid": tip,
              "isCrossRepository": False, "mergeCommit": {"oid": git(r, "rev-parse", "HEAD")}}
        (self.state / "prs").mkdir()
        (self.state / "prs" / "acme__kit-consulting.json").write_text(json.dumps([pr]))
        self.tool("gh", "#!/bin/sh\necho '%s'\n" % json.dumps([dict(pr, updatedAt="2026-11-07T10:00:00Z")]))
        with mock.patch.object(registry, "STATE", self.state):  # the registry's PR cache, here
            rep = self.measure(self.project("acme/kit-consulting", r, [
                self.wt(copy, "stray", "feat/squashed", ahead_default=2, unpushed=2, merged="pr", merged_pr=7,
                        pr={"number": 7, "state": "MERGED"})], github="acme/kit-consulting"))
            self.assertEqual(item(rep, "copy")["safety"], "safe")
            disk.save(rep)
            disk.mark_seen(rep)
            rc, out = self.free(item(rep, "copy")["n"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(copy.exists())
        self.assertEqual(git(r, "branch", "--list", "feat/squashed"), "")

    # Freeing unused images runs Docker's own prune and then the trim, and nothing ever prunes volumes.
    def test_free_images_prunes_images_then_trims_and_never_volumes(self):
        self.fake_colima(on_mac_kb=8)
        self.fake_docker([{"Type": "Images", "TotalCount": "1", "Active": "0", "Size": "30MB",
                           "Reclaimable": "30MB (100%)"},
                          {"Type": "Local Volumes", "TotalCount": "2", "Active": "0", "Size": "5GB",
                           "Reclaimable": "5GB"}],
                         [{"Repository": "semgrep/semgrep", "Tag": "<none>", "ID": "4a34048c04fe", "Size": "30MB"}])
        rep = self.measure()
        disk.save(rep)
        disk.mark_seen(rep)
        rc, out = self.free(item(rep, "images")["n"])
        self.assertEqual(rc, 0, out)
        calls = self.calls()
        prune = calls.index("docker image prune -a -f")
        trim = calls.index("colima ssh -p default -- sudo fstrim -av")
        self.assertLess(prune, trim)
        self.assertFalse([c for c in calls if "volume" in c or "system prune" in c])

    # A cache that a running program uses is refused at that moment, and its tool is not run; pnpm running does not
    # hold the npm cache.
    def test_free_refuses_a_cache_its_tool_is_using(self):
        write(self.home / ".npm" / "_cacache" / "index", 40)
        rep = self.measure()
        disk.save(rep)
        disk.mark_seen(rep)
        n = item(rep, "cache")["n"]
        self.procs = ["/opt/homebrew/bin/node /opt/homebrew/bin/npm install"]
        rc, out = self.free(n)
        self.assertEqual(rc, 1)
        self.assertIn("Not freed: npm cache is in use by a running program", out)
        self.assertFalse([c for c in self.calls() if c.startswith("npm")])
        self.procs = ["pnpm install --frozen-lockfile"]
        rc, out = self.free(n)
        self.assertEqual(rc, 0, out)
        self.assertIn("npm cache clean --force", self.calls())

    # dev free alone prints the plan, records it as seen, and deletes nothing.
    def test_free_alone_prints_the_plan(self):
        r, copy, rep = self.setup_copy()
        rc, out = self.free()
        self.assertEqual(rc, 0)
        self.assertIn("Plan to free space", out)
        self.assertTrue((copy / "dist" / "big.js").exists())
        self.assertTrue(disk.seen_file().exists())

    # dev gc is dev free under its old name; --apply still runs the unattended cleanup of the nightly job.
    def test_gc_is_free_under_its_old_name(self):
        ap = cli.build_parser()
        a = ap.parse_args(["gc", "2"])
        with mock.patch.object(disk, "cmd_free", return_value=0) as free, \
                mock.patch.object(cli, "_gc_unattended", return_value=0) as legacy:
            a.fn(a)
            b = ap.parse_args(["gc", "--apply", "--yes", "--auto-only"])
            b.fn(b)
        self.assertEqual(free.call_args[0][0].items, ["2"])
        legacy.assert_called_once()
        self.assertEqual(ap.parse_args(["free", "3", "--safe"]).items, ["3"])
        subs = next(x for x in ap._actions if isinstance(x, argparse._SubParsersAction))
        gc_help = next(c for c in subs._choices_actions if c.dest == "gc").help
        self.assertIn("the same as dev free", gc_help)


# --------------------------------------------------------------------------- dev session (Kid's continuity.py)


class SessionWiringTests(unittest.TestCase):
    # `dev session` reaches continuity's commands with their arguments, as agreed with Kid.
    def test_dev_session_runs_the_continuity_commands(self):
        from devkit import continuity
        a = cli.build_parser().parse_args(["session", "brief", "--latest", "-p", "acme/api"])
        self.assertIs(a.fn, continuity.cmd_session)
        self.assertEqual((a.session_cmd, a.latest, a.project), ("brief", True, "acme/api"))


# --------------------------------------------------------------------------- estimates in dev progress (M264)


class EstimateTests(unittest.TestCase):
    # An estimate rounds like the dashboard's: to 5 minutes, and to the minute under 10 minutes.
    def test_estimates_round_to_five_minutes_and_to_the_minute_under_ten(self):
        self.assertEqual([cli._about(m) for m in (119, 27.4, 43, 12, 12.5, 7.4, 3.2)],
                         ["2h00m", "25m", "45m", "10m", "15m", "7m", "3m"])


# --------------------------------------------------------------------------- the audit trail


class AuditTests(unittest.TestCase):
    # Listing aliases and showing the plan only read, so they are not audited; setting an alias and freeing are.
    def test_listing_aliases_and_the_plan_is_not_audited_but_freeing_is(self):
        with mock.patch.object(audit, "settings", return_value={"level": "changes"}):
            self.assertFalse(audit.audited(["alias"]))
            self.assertTrue(audit.audited(["alias", "kit", "devkit"]))
            self.assertFalse(audit.audited(["free"]))
            self.assertFalse(audit.audited(["gc"]))
            self.assertFalse(audit.audited(["du", "--refresh"]))
            self.assertTrue(audit.audited(["free", "2"]))
            self.assertTrue(audit.audited(["free", "--safe"]))
            self.assertTrue(audit.audited(["gc", "3"]))
            self.assertTrue(audit.audited(["gc", "--apply", "--yes", "--auto-only"]))
            self.assertTrue(audit.audited(["docker", "trim"]))


if __name__ == "__main__":
    unittest.main()
