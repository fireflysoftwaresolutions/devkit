# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Read-only crew lanes (review, read) run the diagnostics, substitutions, sed and awk forms that only read, mkdir in
# their own scratch folder, a shell variable of their own, and `dev guard check` and `dev lesson show`. The commands
# are ones review members were refused for in recorded sessions. Each allowed form has a twin that writes or runs code
# and stays refused.
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

from devkit import core, guard  # noqa: E402

# the folder the commands run in: a project of the run's dev root (below its temporary home), so a lane's
# verdicts do not follow where this checkout lives (in a copy inside a session scratchpad the guard's
# scratch-folder rule would judge every path first)
CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")


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
        # an inline program that only reads runs (test_guard_false_refusals.ReadOnlyLanes); one that writes does not
        self.assert_refused('python3 -c "open(\'lib/x.py\', \'w\')"', "bash -c 'echo x'", "python3 --version x.py",
                            "bash --version; bash", "node -v -e \"require('fs').rmSync('lib')\"")

    def test_command_v_only_looks_names_up(self):
        self.assert_runs("command -v gh dev", "command -v python3", "command -V git", "command -pv ls")
        # twins: command without -v runs the program, and a program after the lookup is judged on its own
        self.assert_refused("command gh pr merge 12", "command -p gh pr merge 12", "command -v gh; gh pr merge 12",
                            "command -v $(touch x)", "command -- -v gh")

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
                            "$(echo rm) -rf build", 'echo "`touch x`"', "x=$(cat f); echo $((x + 2))",
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
                         "dev lesson show L-abc123", "dev lesson", "dev lesson ls")
        self.assert_refused("dev lesson prevent L-x --by guard --ref t --why w", "dev lesson retire L-x",
                            "dev plan revise x.md --why y")

    def test_a_read_only_lane_runs_no_script_of_its_own(self):
        # no reading of a program's code tells every way it may write (map(Path.write_text, …), sorted(key=
        # Path.unlink), operator.methodcaller, Path.copy): a read-only lane runs no program of its own, even one that
        # only reads; the tech lead runs a probe for it
        (self.pad / "seraph" / "probe.py").write_text("import json, sys\nprint(json.dumps(sys.argv))\n")
        (self.pad / "seraph" / "spawn.py").write_text("import subprocess\nsubprocess.run(['git', 'status'])\n")
        self.assert_refused(f"python3 {self.pad}/seraph/probe.py",
                            f"cd {self.pad}/seraph && python3 probe.py data.json",
                            f"python3 {self.pad}/seraph/spawn.py", f"python3 {self.pad}/seraph/missing.py",
                            f"python3 {self.pad}/tank/probe.py",
                            f"python3 -c 'import runpy, sys; runpy.run_path(sys.argv[1])' {self.pad}/tank/p.py",
                            f"python3 -m http.server {self.pad}/seraph/p.py")


def verdict(cmd, member, level="review", cwd=CWD, base=None):
    """The refusal of a lane's command at tool level `level` (crew_shell_verdict), or None."""
    v = guard.crew_shell_verdict(cmd, str(cwd), None, member, level=level, base=None if base is None else str(base))
    return v[1] if v else None


def _git(*args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class PortfolioReads(unittest.TestCase):
    """The work-management reads (overview, standup, report, team, initiatives, plans) write no record: they run in
    every read-only lane, and their forms that write stay refused."""

    READS = ("dev overview", "dev overview --json --days 3 --all", "dev standup", "dev standup --days 2",
             "dev report", "dev report --days 14 -p acme/shop", "dev report -i payments", "dev team",
             "dev team --hours 4", "dev initiative", "dev initiative ls", "dev initiative show payments",
             "dev plan", "dev plan ls", "dev plan show")
    WRITES = ("dev report -o status.md", "dev report --out=status.md", "dev report --ou status.md",
              "dev report -ostatus.md", "dev report --days 3 -- -o x", "dev initiative new payments",
              "dev initiative add payments acme/shop", "dev initiative rm payments acme/shop",
              "dev plan approve x.md", "dev plan supersede x.md")

    def test_the_reads_run_in_review_and_read_lanes(self):
        for level in ("review", "read"):
            for cmd in self.READS:
                with self.subTest(level=level, cmd=cmd):
                    self.assertIsNone(verdict(cmd, "seraph", level))

    def test_the_forms_that_write_stay_refused_while_their_reads_run(self):
        for level in ("review", "read"):
            self.assertIsNone(verdict("dev report --days 3", "oracle", level))
            self.assertIsNone(verdict("dev initiative show payments", "oracle", level))
            for cmd in self.WRITES:
                with self.subTest(level=level, cmd=cmd):
                    self.assertIn("read-only lane", verdict(cmd, "oracle", level) or "")


class PlanWritesFromReadOnlyLanes(unittest.TestCase):
    """`dev plan new|revise` run in a review lane of a member whose kinds of work produce plans (requirements,
    architecture, research, ux, docs), and write only below plans/<the lane's project>/ of the handoff repository."""

    WRITERS = ("oracle", "architect", "roland", "sati", "ghost")

    @classmethod
    def setUpClass(cls):
        cls.shop, cls.other = core.DEV / "acme" / "shop", core.DEV / "acme" / "other"
        for repo, name in ((cls.shop, "shop"), (cls.other, "other")):
            repo.mkdir(parents=True, exist_ok=True)
            _git("init", "-q", cwd=repo)
            _git("remote", "add", "origin", f"https://github.com/acme/{name}.git", cwd=repo)
        cls.plans = core.HANDOFF / "plans" / "acme" / "shop"
        cls.plans.mkdir(parents=True, exist_ok=True)
        cls.spec = cls.plans / "2026-11-09-checkout.md"
        cls.spec.write_text("# Checkout\n")
        theirs = core.HANDOFF / "plans" / "acme" / "other"
        theirs.mkdir(parents=True, exist_ok=True)
        cls.theirs = theirs / "2026-11-09-billing.md"
        cls.theirs.write_text("# Billing\n")
        (core.HANDOFF / "crew").mkdir(parents=True, exist_ok=True)
        cls.crew = core.HANDOFF / "crew" / "crew.json"
        cls.crew.write_text("{}\n")
        cls.link = cls.plans / "2026-11-09-link.md"
        if not cls.link.is_symlink():
            cls.link.symlink_to(cls.crew)

    def writes(self):
        return ('dev plan new "Checkout spec" --kind spec', "dev plan new Payment retries",
                f"dev plan revise {self.spec} --why 'acceptance criteria'",
                f"git status && dev plan revise {self.spec} --why scope")

    def test_a_plan_write_after_a_change_of_folder_is_refused(self):
        # the guard cannot follow `cd -`, popd, or a cd in a subshell, a pipe or a background job, so a plan write runs
        # alone, from the folder the lane is in; the twin runs after a command that stays in it
        o, s = self.other, self.shop
        self.assertIsNone(verdict("git status && dev plan new x", "oracle", cwd=s))
        for cmd in (f"cd {o} && cd {s} && cd - && dev plan new x", f"cd {o} && (cd {s}) && dev plan new x",
                    f"pushd {o} && popd && dev plan new x", f"cd {o} | true && dev plan new x",
                    f"cd {o} & dev plan new x", f"(cd {o}) ; dev plan new x", f"cd {s} && dev plan new x",
                    f"cd {self.plans} && dev plan revise 2026-11-09-checkout.md --why scope"):
            with self.subTest(cmd=cmd):
                self.assertIn("read-only lane", verdict(cmd, "oracle", cwd=s) or "")

    def test_plan_writers_create_and_revise_their_projects_plans(self):
        for member in self.WRITERS:
            for cmd in self.writes():
                with self.subTest(member=member, cmd=cmd):
                    self.assertIsNone(verdict(cmd, member, cwd=self.shop))

    def test_a_plan_write_outside_the_projects_folder_is_refused(self):
        h = core.HANDOFF
        self.assertIsNone(verdict(f"dev plan revise {self.spec} --why y", "oracle", cwd=self.shop))  # the twin
        for cmd in ("dev plan new x -p acme/other", "dev plan new x --project=acme/shop", "dev plan new x -i payments",
                    "dev plan -p acme/other new x", f"cd {self.other} && dev plan new x", f"cd {h} && dev plan new x",
                    f"dev plan revise {self.theirs} --why y", f"dev plan revise {self.plans}/../other/x.md --why y",
                    f"dev plan revise {self.crew} --why y", f"dev plan revise {h}/projects/acme/shop.md --why y",
                    f"dev plan revise {self.plans}/notes.txt --why y", f"dev plan revise {self.link} --why y",
                    f"dev plan revise {self.spec} {self.theirs} --why y", "dev plan revise $PLAN --why y",
                    f"echo $(dev plan revise {self.spec} --why y)", f"dev plan approve {self.spec}"):
            with self.subTest(cmd=cmd):
                self.assertIn("read-only lane", verdict(cmd, "oracle", cwd=self.shop) or "")

    def test_the_lanes_project_comes_from_the_runs_repository(self):
        # a lane whose shell sits in another project still writes only its run's plans
        self.assertIsNone(verdict(f"dev plan revise {self.spec} --why y", "architect", cwd=self.other, base=self.shop))
        self.assertIsNotNone(verdict("dev plan new x", "architect", cwd=self.other, base=self.shop))
        self.assertIsNotNone(verdict(f"dev plan revise {self.theirs} --why y", "architect", cwd=self.other,
                                     base=self.shop))

    def test_members_whose_work_produces_no_plans_and_read_lanes_are_refused(self):
        self.assertIsNone(verdict("dev plan new x", "oracle", "review", cwd=self.shop))  # the twin: a plan writer
        for member, level in (("seraph", "review"), ("smith", "review"), ("tank", "review"), ("keymaker", "review"),
                              ("sparks", "read"), ("oracle", "read"), (None, "review")):
            for cmd in self.writes():
                with self.subTest(member=member, level=level, cmd=cmd):
                    self.assertIn("read-only lane", verdict(cmd, member, level, cwd=self.shop) or "")


if __name__ == "__main__":
    unittest.main()
