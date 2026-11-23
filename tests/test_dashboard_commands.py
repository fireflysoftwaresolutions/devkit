# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Every command the dashboard builds from data quotes each value for the shell. Branch names, project ids, initiative
slugs, plan paths and CI job names come from the shared handoff repository and from git, which allows ; $ & and quotes
in a branch name: a value pasted unquoted would run a second command. The page from the dashboard tests' fixture, with
hostile names added, in headless Chromium; every command it offers to copy or shows is pasted into /bin/sh, where the
programs it calls only print their arguments. Run: python3 -m unittest tests.test_dashboard_commands -v"""
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import test_dashboard as T  # noqa: E402  (the browser harness and the fixture)
from devkit import dashboard  # noqa: E402

BRANCH = "x;touch pwn"            # a second command after a ;
SUBST = "a $(id)"                 # a command substitution, and a space
HOSTILE = (BRANCH, SUBST)
CLONED = "acme/" + BRANCH         # a project cloned on this Mac
REMOTE = "acme/" + SUBST          # a project not cloned here
SLUG = BRANCH                     # an initiative
PROGRAMS = ("dev", "gh", "git", "dcd", "cd")
# the programs a copied command calls print each argument on its own line, after a marker; a placeholder such as
# <owner/repo> becomes one word, as the developer would replace it before running the command
STUBS = "".join(f"{p}() {{ printf '>%s\\n' {p}; printf '%s\\n' \"$@\"; }}; " for p in PROGRAMS)
PLACEHOLDER = re.compile(r"<[\w/-]+>")

# visits every screen and drawer that builds a command from the hostile names, and keeps each command it offers to copy
# (the title of a copy button) and each command it shows (a code element)
COLLECT = """const seen = []; const keep = () => { seen.push(...$$("button").filter(b => b.querySelector("svg.i-copy")).map(b => b.title).filter(Boolean),
      ...$$("main code, #modal code").map(text)); };
  const proj = (id, q) => "#/projects/" + id.split("/").map(encodeURIComponent).join("/") + (q || "");
  for (const tab of ["", "tasks", "prs", "branches", "worktrees", "plans", "note"]) { await go(proj(%(cloned)s, tab ? "?tab=" + tab : "")); keep(); }
  for (const tab of ["", "branches"]) { await go(proj(%(remote)s, tab ? "?tab=" + tab : "")); keep(); }
  for (const hs of ["#/initiatives", "#/initiatives/lending?tab=projects", "#/initiatives/" + encodeURIComponent(%(slug)s),
                    "#/initiatives/" + encodeURIComponent(%(slug)s) + "?tab=projects", "#/team", "#/tasks", "#/health", "#/now?all=1",
                    "#/background?modal=ci%%3Aacme%%2Fwidget%%40feat%%2Fx", "#/projects?modal=" + encodeURIComponent("project:" + %(remote)s),
                    "#/projects?modal=" + encodeURIComponent("project:" + %(cloned)s), "#/tasks?task=T-c1"]) { await go(hs); keep(); }
  await go("#/now"); document.dispatchEvent(new KeyboardEvent("keydown", {key: "k", metaKey: true, bubbles: true})); await tick(100);
  const inp = $("#palette input"); inp.value = "Copy:"; inp.dispatchEvent(new Event("input", {bubbles: true})); await tick(100);
  seen.push(...$$("#pal-list li .ell").map(text).filter(t => t.startsWith("Copy: ")).map(t => t.slice(6)));
  out.cmds = Array.from(new Set(seen));"""


def hostile_data(root: Path) -> dict:
    """The fixture's page data with hostile names: a cloned project (worktrees on two hostile branches, a task, an
    expired lock, a pull request, a plan to revise, a status saved on another branch, a checkout off its canonical path),
    a project not cloned here, an initiative, and a failed CI job, each named with ; or $( )."""
    base = T.fixture_data(root, T.NOW)
    reg = T.registry()
    widget, api, lending = reg["projects"][0], reg["projects"][1], reg["initiatives"][0]
    wt = lambda branch: dict(widget["worktrees"][1], branch=branch, path="/Users/t/Development/.worktrees/" + CLONED + "/" + branch)  # noqa: E731
    cloned = dict(copy.deepcopy(widget), id=CLONED, name=BRANCH, path="/Users/t/Development/" + CLONED, github=CLONED, note=None,
                  remote="https://github.com/acme/x.git", canonical_ok=False, canonical="/Users/t/Development/acme/x",
                  worktrees=[wt(BRANCH), wt(SUBST)], prs_open=[T.pr(40, "failing", branch=BRANCH), T.pr(41, "passing", branch=SUBST)],
                  status_line={"ts": int(T.NOW - 300), "agent": "claude", "line": T.SAVED, "branch": "feat/q"}, plans=[])
    remote = dict(copy.deepcopy(api), id=REMOTE, name=SUBST, github=REMOTE, note=None)
    reg["projects"] += [cloned, remote]
    lending["projects"] = lending["projects"] + [CLONED, REMOTE]
    lending["members"] = lending["members"] + [dict(lending["members"][1], id=REMOTE), dict(lending["members"][0], id=CLONED)]
    reg["initiatives"].append(dict(copy.deepcopy(lending), id=SLUG, title="Hostile initiative", projects=[], members=[], docs=[],
                                   note="initiatives/x/README.md", tasks={"now": [], "next": [], "later": [], "done_recent": [], "open": 0,
                                                                          "done": 0, "overdue": []}))
    T.item(root, CLONED, "T-c1", "Hostile scope task", "todo", "now")
    T.item(root, CLONED, "T-c2", "Expired lock in a hostile scope", "doing", "now", "bob@studio (codex:def)", T.NOW - 3600)
    T.item(root, "initiatives/" + SLUG, "T-c3", "Hostile initiative task", "doing", "now")
    (root / "plans" / CLONED).mkdir(parents=True)
    (root / "plans" / CLONED / "spec.md").write_text("# Spec edited without a revision\n")
    d = dashboard.collect(reg, handoff=root, now=T.NOW)
    for k in ("runs", "jobs", "ci", "crew"):
        d[k] = base[k]
    d["ci"]["watches"][0]["failures"][0]["jobs"][0]["name"] = SUBST
    return d


def pasted(cmd: str, cwd: Path) -> list:
    """The lines /bin/sh prints for `cmd` when the programs it calls only print their arguments."""
    out = subprocess.run(["/bin/sh", "-c", STUBS + PLACEHOLDER.sub("PLACEHOLDER", cmd)], cwd=cwd, capture_output=True,
                         text=True, timeout=10)
    return out.stdout.splitlines() + ["stderr: " + ln for ln in out.stderr.splitlines()]


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class QuotedCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = hostile_data(Path(cls.tmp.name) / "handoff")
        js = {k: json.dumps(v) for k, v in {"cloned": CLONED, "remote": REMOTE, "slug": SLUG}.items()}
        out = T.browse(dashboard.page(cls.data), COLLECT % js, 1440, "#/now", budget=20000, now=T.NOW)
        cls.error = out.get("probeError") or out.get("pageError")
        cls.cmds = [c for c in out.get("cmds") or [] if c.split(" ", 1)[0] in PROGRAMS]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_each_hostile_name_stays_one_argument_when_the_command_is_pasted(self):
        self.assertIsNone(self.error)
        risky = [c for c in self.cmds if any(v in c for v in HOSTILE)]
        self.assertGreater(len(risky), 20, self.cmds)
        broken = {}
        with tempfile.TemporaryDirectory() as cwd:
            for c in risky:
                lines = pasted(c, Path(cwd))
                whole = all(any(v in ln for ln in lines) for v in HOSTILE if v in c)
                pwn = Path(cwd) / "pwn"
                if not whole or any(ln.startswith("stderr: ") or "uid=" in ln for ln in lines) or pwn.exists():
                    broken[c] = lines
                    pwn.unlink(missing_ok=True)
        self.assertEqual(broken, {})

    def test_the_commands_built_from_branches_ids_slugs_paths_and_job_names_are_quoted(self):
        self.assertIsNone(self.error)
        q = lambda v: "'" + v + "'"  # noqa: E731  (the hostile names hold no single quote)
        for cmd in ("dev wt rm " + q(BRANCH), "dev wt rm " + q(SUBST), "dev clone " + q(REMOTE), "dev claude " + q(CLONED),
                    "dev codex " + q(CLONED), "dcd " + q(CLONED), "dev handoff " + q(CLONED), "dev adopt " + q("/Users/t/Development/" + CLONED),
                    "dev task -p " + q(CLONED) + " start T-c1", "dev task -p " + q(CLONED) + ' release T-c2 --why "lock expired"',
                    "gh pr checks 40 -R " + q(CLONED) + " --watch", "gh pr view 41 -R " + q(CLONED),
                    "dev initiative show " + q(SLUG), "dev task -i " + q(SLUG) + " done T-c3",
                    "dev plan revise " + q(self.data["handoff"] + "/plans/" + CLONED + "/spec.md") + ' --why "…"'):
            self.assertIn(cmd, self.cmds)
        brief = [c for c in self.cmds if c.startswith("dev crew brief link ")]
        self.assertEqual(brief, ["dev crew brief link 'Fix the failed CI run: a $(id), test failure'"])


if __name__ == "__main__":
    unittest.main()
