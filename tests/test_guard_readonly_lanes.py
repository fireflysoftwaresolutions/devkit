# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Read-only crew lanes (review, read) run the diagnostics, substitutions, sed and awk forms that only read, mkdir in
# their own scratch folder, a shell variable of their own, and `dev guard check` and `dev lesson show`. Lessons
# L-fzhjqanjs7z, L-fzmuou5jou0, L-fyzx9w2cwqi, L-fyz5atkx52n and the read-only part of L-fy15osnf0z3: the commands are
# the ones they recorded as refused. Each allowed form has a twin that writes or runs code and stays refused.
# Run: python3 -m unittest tests.test_guard_readonly_lanes -v
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import guard  # noqa: E402

CWD = str(ROOT)


def lane(cmd, member="seraph", cwd=CWD):
    """The refusal of a review lane's command (crew_shell_verdict), or None."""
    v = guard.crew_shell_verdict(cmd, cwd, None, member, level="review")
    return v[1] if v else None


class ReadOnlyLanes(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pad = Path(guard.real(tmp.name)) / "claude-501" / "-x" / "962d6aba-c6a8-44ab-9e02-1d15272468e1" / "scratchpad"
        (self.pad / "seraph").mkdir(parents=True)
        (self.pad / "tank").mkdir()

    def assert_runs(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(lane(cmd))

    def assert_refused(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd))  # the read-only allowlist, or the scratch-folder rule before it

    def test_an_interpreter_or_shell_asked_for_its_version_runs(self):
        self.assert_runs("python3 --version && python3 -m unittest tests.test_guard",
                         "/usr/bin/python3 --version && /usr/bin/python3 -m unittest tests.test_guard",
                         "which -a bash; bash --version", "zsh --version", "node --version", "python3 -V")
        self.assert_refused('python3 -c "print(1)"', "bash -c 'echo x'", "python3 --version x.py",
                            "bash --version; bash", "node -v -e 1")

    def test_the_machine_diagnostics_run_and_sysctl_never_sets_a_value(self):
        self.assert_runs("/usr/sbin/sysctl -n machdep.cpu.brand_string; uname -m", "sysctl hw.ncpu",
                         "ps -ax -o pid,command | grep dev", "pgrep -fl python", "sw_vers", "id -u", "whoami")
        self.assert_refused("sysctl -w kern.maxfiles=1", "sysctl kern.maxfiles=1", "sysctl -f /tmp/x.conf",
                            "sysctl -nw kern.x=1")

    def test_a_substitution_of_a_read_runs(self):
        self.assert_runs("git diff --name-only $(git merge-base HEAD main) HEAD",
                         "diff <(git show a:lib/x.py) <(git show b:lib/x.py)",
                         "for i in $(seq 1 3); do echo $i; done", 'echo "head is `git rev-parse HEAD`"',
                         "wc -l $(git ls-files 'lib/*.py')")
        self.assert_refused("echo $(touch /tmp/pwned)", "cat <(rm -rf build)", "diff <(ls a) >(tee /tmp/out)",
                            "$(echo rm) -rf build", 'echo "`touch x`"', "echo $((1 + 2))",
                            "echo $(echo $(echo $(echo $(ls))))", "ls $(git log | tee /tmp/x)")

    def test_a_continued_line_is_read_as_the_shell_joins_it(self):
        self.assert_runs("python3 -m unittest \\\n  tests.test_guard \\\n  tests.test_guard_crew",
                         "git log --oneline \\\n  -5")
        self.assert_refused("git log \\\n  -5; touch x", "ls # note \\\nrm -rf build", "python3 -m unittest \\\n; rm x")

    def test_a_heredoc_body_hides_no_command_after_it(self):
        # the masked command is tokenised once: a second strip of its heredoc would read the next line as a body
        self.assert_refused("cat <<EOF\nx\nEOF\nrm -rf build", "cat <<'EOF'\n$(x)\nEOF\ntouch y",
                            "cat <<EOF\n$(touch y)\nEOF")
        self.assert_runs("cat <<'EOF'\n$(touch y)\nEOF")  # a quoted delimiter: the body is text

    def test_sed_runs_when_its_script_only_prints(self):
        self.assert_runs("sed 's/foo/bar/g' lib/devkit/guard.py | head", "sed '/^#/d' f", "sed -E 's|a|b|2' f",
                         "sed -n 's/x/y/p' f", "sed -e '1d' -e 's/a/b/' f", "sed 'y/abc/xyz/' f", "sed '$!N;P;D' f",
                         "sed -n '/start/,/end/{p;}' f")
        self.assert_refused("sed -i '' 's/a/b/' f", "sed --in-place 's/a/b/' f", "sed 's/a/b/w /tmp/out' f",
                            "sed 's/a/b/e' f", "sed '1e touch /tmp/x' f", "sed '1r /etc/hosts' f", "sed 'w out' f",
                            "sed -f script.sed f", "sed 's/a/b/ w out' f", "sed 'a\\\nhi' f")

    def test_awk_runs_when_it_only_prints(self):
        self.assert_runs("awk '{print $1}' lib/devkit/guard.py | sort | uniq -c", "awk -F: '$3 < 500 {print $1}' f",
                         "awk -v n=3 'NR<n' f", "gawk 'END {print NR}' f")
        self.assert_refused("awk '{print > \"/tmp/out\"}' f", "awk 'BEGIN{system(\"rm -rf build\")}'",
                            "awk -f prog.awk f", "awk '{print | \"sh\"}' f", "awk '{\"date\" | getline d}' f",
                            "gawk -i inplace '{print}' f", "awk '@load \"filefuncs\"'", "awk")

    def test_mkdir_runs_only_inside_the_members_scratch_folder(self):
        self.assert_runs(f"mkdir -p {self.pad}/seraph/probes", f"mkdir {self.pad}/seraph/r2",
                         f"cd {self.pad}/seraph && mkdir -p a/b")
        self.assert_refused(f"mkdir -p {self.pad}/tank/x", "mkdir -p lib/newdir", f"mkdir -m 777 {self.pad}/seraph/x",
                            f"mkdir -p {self.pad}/seraph-old/../tank/x", "mkdir -p /tmp/x")
        self.assertIn("read-only lane", lane(f"mkdir -p {self.pad}/seraph/x", member=None) or "")

    def test_a_shell_variable_of_its_own_runs_and_the_known_names_stay_refused(self):
        self.assert_runs("S=/private/tmp/x; ls $S", "dir=lib; ls $dir", "P1=a; echo $P1")
        self.assert_refused("PATH=/tmp/evil:$PATH; git log", "PAGER=/tmp/evil; git log", "path=/tmp; git log",
                            "GIT_DIR=/tmp/x; git log", "S=x; export S", "IFS=/; ls")

    def test_devkits_reads_run(self):
        self.assert_runs('dev guard check "rm -rf ~"', "bin/dev guard check --agent codex git push",
                         "dev lesson show L-fy15osnf0z3", "dev lesson", "dev lesson ls")
        self.assert_refused("dev lesson prevent L-x --by guard --ref t --why w", "dev lesson retire L-x",
                            "dev plan revise x.md --why y")

    def test_a_script_from_the_members_own_scratch_folder_runs_and_another_members_does_not(self):
        self.assert_runs(f"python3 {self.pad}/seraph/probe.py", f"cd {self.pad}/seraph && python3 probe.py data.json")
        self.assert_refused(f"python3 {self.pad}/tank/probe.py", f"python3 -c 'x' {self.pad}/seraph/p.py",
                            f"python3 -m http.server {self.pad}/seraph/p.py")


if __name__ == "__main__":
    unittest.main()
