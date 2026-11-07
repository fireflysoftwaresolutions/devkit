# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# F7 (spec v17, security consultation §4): a review or read-only crew member's shell runs only what the read-only
# allowlist names, every segment of it; write detection is the backstop for what an allowed check writes; a lane's
# scope is relative to its run's repository whatever folder the session runs in; and Morpheus may not edit a file that
# a member of another session's live run owns. The guard reads command text and paths: no command here runs.
# Run: python3 -m unittest tests.test_shell_allowlist -v
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

from devkit import core, guard  # noqa: E402

CWD = str(ROOT)
REVIEWER = "seraph"  # a review-only member in config/crew.json: its lane gets the allowlist without a level


def lane(cmd, member=REVIEWER, cwd=CWD, **kw):
    v = guard.crew_shell_verdict(cmd, cwd, None, member, **kw)
    return v[1] if v else None


# the main session works in a project of the dev root (the run's, below its temporary home), where the guard allows
# its writes, whatever folder this checkout lives in (~/Development on the developer's Mac, a runner folder in CI)
MAIN_CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")


def main_session(cmd, cwd=MAIN_CWD):
    v = guard.check("Bash", {"command": cmd}, cwd, "claude")
    return v[0] if v else "allow"


# every form the consultation lists as a writer, plus the nested shells and interpreters: each one is refused in a
# review lane, and the main session's guard (which F7 does not change) still allows it
WRITERS = [
    "perl -i -pe 's/a/b/' src/a.py", "perl -pi -e 's/a/b/' src/a.py", "ruby -i -pe 'gsub(/a/, %q(b))' src/a.py",
    "gawk -i inplace '{print}' src/a.py", "awk '{print > \"out.txt\"}' src/a.py", "awk 'BEGIN {system(\"touch x\")}'",
    "sed -n '1w out.txt' src/a.py", "sed -n '/x/W out.txt' src/a.py", "sed -n '1e touch x' src/a.py",
    "sed -i '' 's/a/b/' src/a.py", "python3 -c \"open('src/a.py', 'w').write('x')\"",
    "python3 - <<'EOF'\nprint(1)\nEOF", "node -e \"require('fs').writeFileSync('x', '')\"",
    "ruby -e 'File.write(%q(x), %q(y))'", "perl -e 'print 1'", "php -r 'file_put_contents(\"x\", \"y\");'",
    "osascript -e 'do shell script \"touch x\"'", "bash -c 'cat src/a.py'", "sh -c 'echo x > f'", "zsh -c ls",
    "eval 'ls'", "source ./env.sh", "ls | xargs touch", "find . -name '*.py' -exec sed -i s/a/b/ {} +",
    "find . -fprint out.txt", "fd -e py -x sed -i s/a/b/", "rg --pre ./decode.sh secret", "git apply fix.patch",
    "git am fix.patch", "git cherry-pick abc123", "git merge main", "git rebase main", "git pull",
    "git switch -c feat/x", "git add src/a.py", "git rm src/a.py", "git mv a b", "git config user.name x",
    "git update-ref refs/heads/x HEAD", "git notes add -m x", "git -c core.fsmonitor=./x.sh status",
    "git diff --output=out.txt", "git log --output out.txt", "git format-patch -o out HEAD~1",
    "git archive -o out.zip HEAD", "patch -p1 < fix.patch", "dd if=a of=b", "truncate -s 0 src/a.py",
    "install -m 644 a b", "ln -s a b", "chmod +x a", "touch a", "mkdir build", "curl -o out https://example.com",
    "curl -O https://example.com/x", "wget https://example.com", "tar -xf a.tar", "unzip a.zip", "rsync -a a/ b/",
    "scp a host:b", "sort -o out.txt a", "split -l 10 a", "yq -i '.a = 1' a.yml", "ruff check --fix .",
    "ruff format .", "eslint --fix src", "prettier --write .", "black .", "pip install x", "npm install",
    "npx cowsay hi", "make", "dev rm a", "dev mv a b", "dev cp a b", "dev commit -m 'x'",
    "sqlite3 db.sqlite 'delete from t'", "docker run -v \"$PWD:/src\" alpine touch /src/x", "echo x >| out.txt",
    "ls &> out.txt", "exec > out.txt", "ls > >(cat)", "bash -c \"echo x > f\"", "echo x > notes.md",
    "tee out.txt < a", "cp a b", "mv a b", "tsc", "semgrep --autofix .", "markdownlint --fix .", "uniq a b",
    "xxd a b", "tree -o out.txt", "git reflog expire --all", "gh pr comment 12 -b x",
    "gh api -X POST repos/o/r/issues", "gh api repos/o/r/issues -f title=x", "gh run download 5",
    "dev task add 'x'", "dev ci --run", "cat $(echo a)", "cat `echo a`", "diff <(ls a) <(ls b)",
    "GIT_EXTERNAL_DIFF=./x.sh git diff", "sudo cat a",
]
# writers the main session's guard refuses for its own reasons (deletes, branch and tag names, the shared stash)
MAIN_REFUSES = ["find . -name '*.pyc' -delete", "git clean -fd", "git tag v9.9.9", "git stash", "git branch x",
                "gh pr merge 12"]


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        # the main session's project exists, as a real checkout does (its owner folder is what the guard looks for)
        owner = Path(MAIN_CWD).parent
        if not owner.exists():
            self.addCleanup(shutil.rmtree, owner, True)
        Path(MAIN_CWD).mkdir(parents=True, exist_ok=True)

    def test_every_writer_form_is_refused_in_a_review_lane_and_allowed_in_the_main_session(self):
        for cmd in WRITERS:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))
                self.assertEqual(main_session(cmd), "allow")
        for cmd in MAIN_REFUSES:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))

    def test_sed_prints_a_line_range_or_a_match_but_never_writes_or_runs(self):
        for cmd in ("sed -n '1,20p' f", "sed -n '/def check/p' lib/devkit/guard.py", "sed -n '$p' f",
                    "sed -n -e '1p' -e '3,4p' f", "sed -ne '10,20p' f", "sed -n '120,$p' f", "sed -En '/^[a-z]+$/p' f",
                    "sed -n '1p;5p' f"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))
        for cmd in ("sed -n '1w out' f", "sed -n '1,5p;w x' f", "sed -n 's/a/b/w out' f", "sed -n '1e date' f",
                    "sed '1,5p' f", "sed -n -f script.sed f", "sed -n -i '1p' f", "sed -n --in-place '1p' f"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))

    def test_checks_run_in_a_review_lane_and_their_fix_flags_do_not(self):
        for cmd in ("pytest -q", "uv run pytest -q tests", "python3 -m unittest discover -s tests",
                    "python3 -m unittest tests.test_guard_crew -v", "/usr/bin/python3 -m unittest tests.test_x",
                    "npm test", "pnpm test", "npx vitest run", "./mvnw verify", "./gradlew test", "go test ./...",
                    "cargo test", "ruff check lib", "mypy lib", "tsc --noEmit", "eslint src", "prettier --check .",
                    "shellcheck bin/x.sh", "gitleaks detect --no-banner", "timeout 600 pytest -x",
                    "cd lib && python3 -m unittest discover -s ../tests 2>&1 | tail -20"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))
        for cmd in ("ruff check --fix lib", "eslint --fix src", "prettier --write .", "semgrep --autofix .",
                    "markdownlint --fix docs", "tsc", "pytest -q > report.txt", "npm run build"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))

    def test_a_reviewer_runs_devkits_test_runner_and_a_probe_script_from_its_own_scratch_folder(self):
        pad = "/private/tmp/claude-501/-Users-x/962d6aba-c6a8-44ab-9e02-1d15272468e1/scratchpad"
        # dev test prove runs the tests in a temporary copy of the checkout, restores the base there and removes it:
        # the checkout stays as it was, so a reviewer can check that new tests fail without the change (Seraph, B3)
        for cmd in ("bin/dev test", "dev test run --failed", f"python3 {pad}/seraph/probe.py",
                    f"python3 -u {pad}/seraph-f7/probe.py --verbose", "dev test prove", "bin/dev test prove --since main",
                    "dev test prove tests/test_gate_tiers.py --json"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))
        for cmd in (f"python3 {pad}/tank/probe.py", f"python3 {pad}/probe.py",
                    f"python3 -c 'import runpy; runpy.run_path(\"{pad}/seraph/probe.py\")'"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))

    def test_the_developer_adds_checks_in_safeguards_readonly_commands(self):
        with mock.patch.object(guard, "_readonly_config", lambda: ["make lint", "just check"]):
            self.assertIsNone(lane("make lint"))
            self.assertIsNone(lane("just check --quiet"))
            self.assertIsNotNone(lane("make install"))

    def test_reading_commands_and_redirects_to_dev_null_or_a_temp_folder_run(self):
        for cmd in ("cat lib/devkit/guard.py | head -50", "grep -rn 'def check' lib", "rg -n crew_shell lib",
                    "find lib -name '*.py' -newer README.md", "ls -la tests", "wc -l lib/devkit/*.py",
                    "git status --porcelain", "git log --oneline -12", "git diff HEAD~1 -- lib", "git show --stat HEAD",
                    "git blame -L 1,20 lib/devkit/guard.py", "git branch -a", "git tag -l 'v26*'", "git remote -v",
                    "git stash list", "git worktree list", "git config --get user.name", "git -C lib log -1",
                    "gh pr view 12 --json title", "gh pr diff 12", "gh pr checks 12", "gh run view 5 --log-failed",
                    "gh api repos/o/r/pulls", "bin/dev status", "dev task", "dev task show 4", "dev grep guard",
                    "dev diff --names", "dev sec scan", "dev test plan", "dev plan show x", "dev crew raci",
                    "dev lesson add 'x' --cause y --prevent z", "jq '.a' f.json", "yq '.a' f.yml",
                    "git grep -n crew_shell 2>/dev/null", "ls > /tmp/listing.txt", "LC_ALL=C sort f",
                    "GIT_PAGER=cat git log -3", "for f in lib/devkit/*.py; do wc -l \"$f\"; done",
                    "find lib -name '*.py' -exec grep -l crew {} +", "git ls-files | xargs wc -l",
                    "diff -u a b || true", "echo done", "cd tests && ls"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))
        self.assertIsNotNone(lane("ls > listing.txt"))  # a redirect into the repository writes a file

    def test_a_review_lane_reads_the_audit_trail_but_never_publishes_or_exports_it_to_a_file(self):
        # Persephone's lesson L-fxwm448qq5i (Morpheus, M163): the audit reads are read commands
        for cmd in ("dev audit", "dev audit verify", "dev audit stats --days 3", "dev audit show 1a2b",
                    "dev audit config", "bin/dev audit --action guard --json"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))
        for cmd in ("dev audit --publish", "dev audit export -o out.jsonl", "dev audit export --out=out.jsonl",
                    "dev audit export"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))

    def test_git_and_gh_read_but_never_change_anything(self):
        for cmd in ("git branch x", "git branch -d x", "git tag -d v1", "git remote add o url", "git stash pop",
                    "git worktree add ../x", "git config --unset x", "git config core.hooksPath x",
                    "git grep -O vim x", "git fetch", "gh pr merge 12", "gh api --method PATCH repos/o/r",
                    "gh api graphql -f query=x", "gh release download v1", "gh pr checkout 12", "gh auth token"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))
        self.assertIsNone(lane("git log -1 && gh pr view 12"))

    def test_an_unparseable_command_is_refused(self):
        self.assertIsNotNone(lane("cat 'unterminated"))
        self.assertIsNone(lane("cat 'terminated'"))

    def test_variables_that_make_a_reader_run_a_program_are_refused(self):
        for cmd in ("GIT_EXTERNAL_DIFF=./x.sh git diff", "GIT_PAGER='sh -c x' git log", "PAGER=./x git log",
                    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=./x git status",
                    "LESSOPEN='|./x %s' git log", "export GIT_EXTERNAL_DIFF=./x; git diff"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))
        self.assertIsNone(lane("NO_COLOR=1 TZ=UTC pytest -q"))

    def test_the_refusal_tells_the_member_what_to_do_instead(self):
        why = lane("sed -i '' s/a/b/ lib/devkit/guard.py")
        self.assertIn("read-only", why)
        self.assertIn("file:line", why)
        self.assertIn("ask the tech lead", why)

    def test_a_build_lane_keeps_its_shell_and_an_explicit_level_wins_over_the_member(self):
        self.assertIsNone(lane("sed -i '' s/a/b/ src/a.py", member="keymaker"))  # keymaker is a build member
        self.assertIsNone(lane("touch src/a.py", member="a1b2c3d4", level="all"))
        self.assertIsNotNone(lane("touch src/a.py", member="a1b2c3d4", level="read"))
        self.assertIsNotNone(lane("touch src/a.py", member="keymaker", level="review"))  # a helper of a reviewer

    def test_a_lookup_failure_makes_the_lane_read_only_and_is_recorded(self):
        # the roster cannot be read: the lane fails closed at review level, and the real _record writes why
        import json
        from devkit import audit
        self.assertIsNone(lane("touch src/a.py", member="keymaker", level="all"))  # baseline: the roster reads
        with tempfile.TemporaryDirectory() as d, mock.patch.object(audit, "AUDIT", Path(d) / "audit"), \
                mock.patch("devkit.crew.member", side_effect=RuntimeError("roster unreadable")):
            self.assertIn("read-only", lane("touch src/a.py", member="keymaker", level="all") or "")
            self.assertIsNone(lane("git status", member="keymaker", level="all"))
            recs = [json.loads(line) for f in sorted((Path(d) / "audit").glob("*.jsonl"))
                    for line in f.read_text().splitlines() if line.strip()]
        hits = [r for r in recs if r.get("action") == "guard" and r.get("target") == "crew-readonly"]
        self.assertTrue(hits, recs)
        self.assertIn("roster unreadable", hits[0].get("detail", ""))


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(d):
    repo = Path(d) / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("a = 1\n")
    (repo / "notes.md").write_text("x\n")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


class WriteDetectionTests(unittest.TestCase):

    def test_a_test_run_that_writes_into_src_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            (repo / "notes.md").write_text("changed before the call\n")
            before = guard.worktree_state(str(repo))
            (repo / "src" / "a.py").write_text("a = 2\n")
            (repo / "src" / "new.py").write_text("x = 1\n")
            (repo / "notes.md").write_text("changed again by the call, longer\n")  # a new size, whatever the clock
            (repo / "build").mkdir()
            (repo / "build" / "out.o").write_text("ignored\n")
            self.assertEqual(guard.written_paths(before, guard.worktree_state(str(repo))),
                             ["notes.md", "src/a.py", "src/new.py"])

    def test_a_reverted_file_counts_and_an_untouched_tree_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            (repo / "src" / "a.py").write_text("a = 2\n")
            before = guard.worktree_state(str(repo))
            self.assertEqual(guard.written_paths(before, guard.worktree_state(str(repo))), [])
            _git(repo, "checkout", "--", "src/a.py")
            self.assertEqual(guard.written_paths(before, guard.worktree_state(str(repo))), ["src/a.py"])

    def test_outside_a_repository_there_is_nothing_to_compare(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(guard.worktree_state(d))
            self.assertEqual(guard.written_paths(None, None), [])


class ScopeBaseTests(unittest.TestCase):

    def test_scope_globs_are_relative_to_the_runs_repository_whatever_the_session_folder(self):
        path = str(ROOT / "lib" / "devkit" / "guard.py")
        home = str(core.HOME)
        self.assertFalse(guard.scope_holds(path, home, ["lib/devkit/guard.py"]))  # what the session's cwd gives
        # the workspace and the run's project id that name this checkout, wherever it sits (a main checkout at
        # <dev>/<owner>/<repo> or a managed worktree below .worktrees/)
        with mock.patch.object(guard, "DEV", ROOT.parent):
            base = guard.scope_base(ROOT.name, home)
            self.assertEqual(os.path.realpath(base), os.path.realpath(ROOT))
            self.assertTrue(guard.scope_holds(path, home, ["lib/devkit/guard.py"], base=base))
            self.assertFalse(guard.scope_holds(path, home, ["tests/**"], base=base))
            self.assertEqual(os.path.realpath(guard.scope_base(ROOT.name, CWD + "/lib")), os.path.realpath(ROOT))

    def test_a_session_in_a_worktree_of_the_runs_repository_counts_from_that_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            wt = Path(d) / "wt"
            _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
            with mock.patch.object(guard, "DEV", Path(d)):
                self.assertEqual(os.path.realpath(guard.scope_base("repo", str(wt / "src"))), os.path.realpath(wt))
                self.assertEqual(os.path.realpath(guard.scope_base("repo", d)), os.path.realpath(repo))
                self.assertEqual(os.path.realpath(guard.scope_base("", str(wt))), os.path.realpath(wt))  # no run

    def test_a_plan_named_by_the_scope_is_exempt_from_a_session_in_home(self):
        plan = core.HANDOFF / "plans" / "fireflysoftwaresolutions" / "devkit" / "f7-probe.md"
        scope = [str(core.HANDOFF / "plans" / "fireflysoftwaresolutions" / "devkit") + "/**"]
        with mock.patch.object(guard, "DEV", ROOT.parent):
            base = guard.scope_base(ROOT.name, str(core.HOME))
            self.assertTrue(guard.member_write_exempt(str(plan), str(core.HOME), scope, "oracle", base=base))
            self.assertFalse(guard.member_write_exempt(str(ROOT / "README.md"), str(core.HOME), scope, "oracle",
                                                       base=base))


class OwnershipAcrossRunsTests(unittest.TestCase):

    def _runs(self, repo, state="running", session="s-other"):
        return [{"session": session, "agent": "claude", "project": "o/repo", "plan": [
                    {"name": "api", "owner": "a1", "state": "running", "scope": ["src/api/**"]}],
                 "agents": {"a1": {"state": state, "label": "Tank: the API", "persona": "Tank",
                                   "scope": ["src/models.py"]}}}]

    def test_another_sessions_member_scope_refuses_morpheuss_edit(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            runs = self._runs(repo)
            own = guard.scope_owner_across(str(repo / "src" / "api" / "x.py"), runs, "s-me",
                                           checkout_of=lambda r: str(repo))
            self.assertEqual(own[0], "Tank")
            self.assertIn("src/api/**", own[1])
            self.assertEqual(guard.scope_owner_across(str(repo / "src" / "models.py"), runs, "s-me",
                                                      checkout_of=lambda r: str(repo))[0], "Tank")
            self.assertIsNone(guard.scope_owner_across(str(repo / "notes.md"), runs, "s-me",
                                                       checkout_of=lambda r: str(repo)))

    def test_a_finished_lane_its_own_session_or_another_checkout_owns_nothing_here(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            path = str(repo / "src" / "api" / "x.py")
            self.assertIsNone(guard.scope_owner_across(path, self._runs(repo, state="done"), "s-me",
                                                       checkout_of=lambda r: str(repo)))
            self.assertIsNone(guard.scope_owner_across(path, self._runs(repo, session="s-me"), "s-me",
                                                       checkout_of=lambda r: str(repo)))
            self.assertIsNone(guard.scope_owner_across(path, self._runs(repo), "s-me",
                                                       checkout_of=lambda r: str(Path(d) / "elsewhere")))


if __name__ == "__main__":
    unittest.main()
