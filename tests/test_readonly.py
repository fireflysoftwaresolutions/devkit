# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Whether a shell command can write files (lib/devkit/readonly.py): every part of it must be a reader, a read of git, gh
# or dev, or one of the project's checks without its fix flags, with redirects only to /dev/null, a standard stream or
# a temporary folder; anything else, a program it does not know included, counts as a write. The crew gates attribute
# writes and the call ledger marks calls with it. No command runs.
# Run: python3 -m unittest tests.test_readonly -v
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, readonly  # noqa: E402

CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")

# writers: in-place editors, interpreters and nested shells, file-writing flags, git and gh commands that change
# something, copies and moves, devkit commands that change records, writing substitutions and redirects
WRITERS = [
    "perl -i -pe 's/a/b/' src/a.py", "gawk -i inplace '{print}' src/a.py", "awk '{print > \"out.txt\"}' src/a.py",
    "awk 'BEGIN {system(\"touch x\")}'", "sed -n '1w out.txt' src/a.py", "sed -n '1e touch x' src/a.py",
    "sed -i '' 's/a/b/' src/a.py", "sed -n --in-place '1p' f", "python3 -c \"open('src/a.py', 'w').write('x')\"",
    "python3 - <<'EOF'\nopen('src/a.py', 'w').write('x')\nEOF", "node -e \"require('fs').writeFileSync('x', '')\"",
    "bash -c 'cat src/a.py'", "sh -c 'echo x > f'", "eval 'ls'", "source ./env.sh", "ls | xargs touch",
    "find . -name '*.py' -exec sed -i s/a/b/ {} +", "find . -fprint out.txt", "find . -delete",
    "rg --pre ./decode.sh secret", "git apply fix.patch", "git merge main", "git pull", "git add src/a.py",
    "git -c core.fsmonitor=./x.sh status", "git diff --output=out.txt", "git push", "git branch x", "git fetch",
    "patch -p1 < fix.patch", "dd if=a of=b", "chmod +x a", "touch a", "mkdir build", "curl -o out https://example.com",
    "tar -xf a.tar", "sort -o out.txt a", "yq -i '.a = 1' a.yml", "ruff check --fix .", "eslint --fix src",
    "prettier --write .", "tsc", "npx cowsay hi", "make", "dev commit -m 'x'", "dev task add 'x'", "dev ci --run",
    "dev plan new x", "dev audit --publish", "dev docs check --fix", "echo x >| out.txt", "ls &> out.txt",
    "echo x > notes.md", "tee out.txt < a", "cp a b", "mv a b", "rm -f x", "uniq a b", "gh pr comment 12 -b x",
    "gh api -X POST repos/o/r/issues", "gh api repos/o/r/issues -f title=x", "cat $(touch a)", "cat `touch a`",
    "diff <(touch a) <(ls b)", "GIT_EXTERNAL_DIFF=./x.sh git diff", "GIT_PAGER='sh -c x' git log", "sudo cat a",
    "ls | xargs dev", "dev impeccable live", "dev impeccable detect https://example.com", "echo $((x + 1))",
    "pytest -p plugin", "cat 'unterminated",
]
# reads: readers, git and gh reads, devkit's reading commands, the project's checks, crew calls, writes only to
# /dev/null or a temporary folder, and substitutions whose commands read
READERS = [
    "cat lib/devkit/hooks.py | head -50", "grep -rn 'def check' lib", "rg -n crew lib", "ls -la tests",
    "find lib -name '*.py' -newer README.md", "find lib -name '*.py' -exec grep -l crew {} +", "wc -l lib/devkit/*.py",
    "git status --porcelain", "git log --oneline -12", "git diff HEAD~1 -- lib", "git -C lib log -1",
    "gh pr view 12 --json title", "gh pr checks 12", "gh api repos/o/r/pulls", "bin/dev status", "dev task show 4",
    "dev test plan", "dev lesson add 'x' --cause y --prevent z", "dev progress step x", "dev crew ask y",
    "dev watch --wait", "dev impeccable detect src/", "jq '.a' f.json", "sed -n '1,20p' f", "sed -n '/def x/p' f",
    "pytest -q", "python3 -m unittest discover -s tests", "npm test", "ruff check lib", "tsc --noEmit",
    "prettier --check .", "timeout 600 pytest -x", "LC_ALL=C sort f", "GIT_PAGER=cat git log -3",
    "for f in lib/devkit/*.py; do wc -l \"$f\"; done", "git ls-files | xargs wc -l", "diff -u a b || true",
    "echo done", "cd tests && ls", "ls > /dev/null 2>&1", "ls > /tmp/listing.txt", "cat $(echo a)", "cat `echo a`",
    "diff <(ls a) <(ls b)", "echo $(git rev-parse HEAD)", "awk 'NR>2 {print $1}' a", "bash -n x.sh",
    "python3 --version", "command -v gh dev",
]


class WriteReasonTests(unittest.TestCase):

    def test_every_writer_form_has_a_reason(self):
        for cmd in WRITERS:
            with self.subTest(cmd=cmd):
                self.assertTrue(readonly.write_reason(cmd, CWD))

    def test_every_reader_form_reads(self):
        for cmd in READERS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(readonly.write_reason(cmd, CWD))

    def test_the_reason_names_what_writes(self):
        self.assertEqual(readonly.write_reason("echo x > notes.md", CWD), "the redirect to `notes.md` writes a file")
        self.assertEqual(readonly.write_reason("ruff check --fix", CWD), "`ruff check --fix` writes files")
        self.assertEqual(readonly.write_reason("git status && cp a b", CWD), "`cp` is not on the read-only allowlist")
        self.assertEqual(readonly.write_reason("ls | xargs dev", CWD), "`xargs dev` takes devkit's command from its input")


class ProgramTests(unittest.TestCase):

    def test_one_commands_words_prefixes_included(self):
        self.assertIsNone(readonly.program_write_reason(["git", "status"], CWD))
        self.assertIsNone(readonly.program_write_reason(["env", "LANG=C", "sort", "f"], CWD))
        self.assertEqual(readonly.program_write_reason(["cp", "a", "b"], CWD), "`cp` is not on the read-only allowlist")
        self.assertEqual(readonly.program_write_reason(["sudo", "ls"], CWD), "`sudo` runs the command as another user")

    def test_the_projects_checks_and_their_writing_forms(self):
        self.assertEqual([readonly.check_refusal(argv) for argv in (["pytest"], ["ls"], ["tsc"],
                                                                     ["prettier", "--check", "."])],
                         [None, "", "`tsc` without --noEmit writes the compiled files", None])


class NestedShellTests(unittest.TestCase):

    def test_csh_and_tcsh_take_the_word_right_after_c_and_other_shells_skip_its_options(self):
        # csh -c -e 'line' runs `-e` and fails; bash runs the line after its options
        self.assertEqual(readonly._shell_line(["-c", "-e", "gh pr merge 12"], "csh"), "-e")
        self.assertEqual(readonly._shell_line(["-c", "-e", "gh pr merge 12"], "tcsh"), "-e")
        self.assertEqual(readonly._shell_line(["-c", "-e", "gh pr merge 12"], "bash"), "gh pr merge 12")


class RedirectTests(unittest.TestCase):

    def test_a_redirect_reads_only_into_dev_null_a_stream_or_a_temporary_folder(self):
        for target in ("/dev/null", "/dev/stderr", "-", "/tmp/out.txt"):
            with self.subTest(target=target):
                self.assertTrue(readonly.redirect_reads(target, CWD))
        for target in ("notes.md", "$(mktemp)", "`mktemp`", "src/out.txt"):  # a substitution: known only at run time
            with self.subTest(target=target):
                self.assertFalse(readonly.redirect_reads(target, CWD))


if __name__ == "__main__":
    unittest.main()
