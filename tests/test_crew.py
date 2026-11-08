# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev crew`: the roster (config/crew.json), the generated Claude Code agents and Codex roles, briefs, crew messages,
guided and auto mode with decisions, and the devkit_crew MCP tool. Hermetic: progress state lives in a temporary
folder, agent files are written to a temporary folder, and nothing touches the network."""
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from devkit import audit, canary, cli, core, crew, hooks, mcp, selfcheck, ui  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState as ProgressState  # noqa: E402

try:
    import tomllib  # Python 3.11+
except ImportError:  # Python 3.9
    tomllib = None


def member(**kw) -> dict:
    m = {"id": "pat", "name": "Pat", "title": "Test engineer", "role": "Test engineer", "summary": "Writes the tests that matter.",
         "personality": "Curious.", "use_when": ["missing tests"], "skills": ["testing"], "tools": "all",
         "tier": "standard", "returns": "the tests and their results", "goal": "Tests that catch real bugs.",
         "procedure": [{"phase": "Map", "do": "Map what can break."},
                       {"phase": "Test", "do": "Write one test per behavior.", "devkit": ["dev test"], "proof": "dev test"}],
         "devkit": ["dev test"],
         "done_when": ["every new test fails without the change"]}
    m.update(kw)
    return m


def roster(*members, rule="Write in American English.") -> dict:
    members = list(members) or [member()]
    workers = [m["id"] for m in members if isinstance(m, dict) and not m.get("main") and m.get("id")]
    return {"language_rule": rule, "members": members, "warnings": [],  # each member does its own kind of work
            "activities": [{"id": f"work-{w}", "name": f"Work of {w}", "R": [w], "A": w, "C": [], "I": [],
                            "signals": [f"{w} work"], "self_approval": "a test roster: nobody else to approve"}
                           for w in workers]}


def frontmatter(text: str) -> dict:
    head = text.split("\n---\n", 1)[0].splitlines()[1:]
    return {k: v for k, v in (ln.split(": ", 1) for ln in head)}


def run_cli(*argv, from_agent=None):
    """`dev crew …` through the real parser and handler -> (exit code, stdout, stderr) without colour codes."""
    a = cli.build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    saved = os.environ.pop("DEVKIT_FROM_AGENT", None)
    if from_agent:
        os.environ["DEVKIT_FROM_AGENT"] = from_agent
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = a.fn(a) or 0
    finally:
        os.environ.pop("DEVKIT_FROM_AGENT", None)
        if saved is not None:
            os.environ["DEVKIT_FROM_AGENT"] = saved
    return rc, ui.ANSI_RE.sub("", out.getvalue()), ui.ANSI_RE.sub("", err.getvalue())


class TempState(ProgressState):
    """test_progress's TempState (progress state and the audit trail in a temporary folder, session claude:s1), plus
    devkit's default mode and parallel limit (the developer's own config stays out) and no inherited
    DEVKIT_FROM_AGENT."""

    def __enter__(self):
        super().__enter__()
        self.cfg, self.from_agent = core.CFG, os.environ.pop("DEVKIT_FROM_AGENT", None)
        core.CFG = {k: v for k, v in core.CFG.items() if k not in ("work", "progress")}
        return self

    def __exit__(self, *exc):
        core.CFG = self.cfg
        if self.from_agent is not None:
            os.environ["DEVKIT_FROM_AGENT"] = self.from_agent
        return super().__exit__(*exc)

    @staticmethod
    def launch(agent_id="a1", kind="devkit:neo", description="Neo: build the parser"):
        """A subagent starts in this session (what the hooks record when the Agent tool runs: the call, the agent's
        start, the call's result naming the agent)."""
        pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": f"toolu_{agent_id}",
                                        "tool_input": {"description": description, "subagent_type": kind}})
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": agent_id, "agent_type": kind}, True)
        pg.launch_result("claude", {"session_id": "s1", "tool_use_id": f"toolu_{agent_id}",
                                    "tool_response": {"agentId": agent_id}})

    @staticmethod
    def audit_actions() -> list:
        return [json.loads(ln)["action"] for f in sorted(audit.AUDIT.glob("*.jsonl")) for ln in f.read_text().splitlines()]


# --------------------------------------------------------------------------- roster

class RosterTests(unittest.TestCase):
    def test_the_shipped_roster_is_valid(self):
        r = crew.load()
        self.assertEqual(len(r["members"]), 26)
        self.assertEqual([m["id"] for m in r["members"] if m.get("main")], ["morpheus"])
        self.assertTrue(all("unknown skill" in w for w in r["warnings"]), r["warnings"])

    def test_bad_rosters_are_refused_with_every_problem_named(self):
        good = member()
        cases = {
            "duplicate id": [good, member(name="Other")],
            "kebab-case": [member(id="Pat_One")],
            "missing summary, use_when": [{k: v for k, v in good.items() if k not in ("summary", "use_when")}],
            "tools must be one of": [member(tools="write")],
            "tier must be one of": [member(tier="huge")],
            "only one member can be `main`": [member(main=True), member(id="sam", name="Sam", main=True)],
            "'pat' already names": [good, member(id="sam", name="Pat")],
            "already names": [good, member(id="sam", name="Sam", aliases=["pat"])],
            "`use_when` must be a list of text": [member(use_when="missing tests")],
            "`keywords` must be a list of text": [member(keywords="tests")],
        }
        with tempfile.TemporaryDirectory() as d:
            for needle, members in cases.items():
                with self.subTest(needle=needle):
                    p = Path(d) / "crew.json"
                    p.write_text(json.dumps({"language_rule": "x", "members": members}))
                    with self.assertRaises(crew.RosterError) as e:
                        crew.load(p)
                    self.assertIn(needle, str(e.exception))
            for text, needle in (("{not json", "not valid JSON"), ('{"language_rule": "x", "members": []}', "non-empty"),
                                 (json.dumps({"members": [good]}), "`language_rule` is missing")):
                with self.subTest(needle=needle):
                    p = Path(d) / "crew.json"
                    p.write_text(text)
                    with self.assertRaises(crew.RosterError) as e:
                        crew.load(p)
                    self.assertIn(needle, str(e.exception))
            with self.assertRaises(crew.RosterError):
                crew.load(Path(d) / "missing.json")

    def test_unknown_skills_are_warnings_not_errors(self):
        m = member(skills=["testing", "java-junit", "superpowers:anything", "devkit:testing", "nope", "devkit:nope",
                           "other-plugin:x"], extra=1, keywords=["test", "coverage"])
        errors, warnings = crew.check(dict(roster(m), language_rule="x"))
        self.assertEqual(errors, [])
        self.assertEqual([w.split("`")[3] for w in warnings if "unknown skill" in w],
                         ["nope", "devkit:nope", "other-plugin:x"])
        self.assertIn("unknown field `extra`", " ".join(warnings))

    # an activity's `files` are globs that anchor its steps (raci.anchor), and `no_files` says why it has none;
    # anything else is refused, so a typo cannot widen or silence a kind of work's anchor
    def test_an_activitys_files_are_a_list_of_globs_and_no_files_is_its_reason(self):
        r = roster()
        act = r["activities"][0]
        for field, bad, needle in (("files", "*.po", "`files` must be a list of globs"),
                                   ("files", ["*.po", " "], "`files` must be a list of globs"),
                                   ("no_files", ["none"], "`no_files` must be the reason, as text")):
            with self.subTest(field=field, bad=bad):
                errors, _ = crew.check(dict(r, activities=[dict(act, **{field: bad})]))
                self.assertIn(needle, " ".join(errors))
        good = dict(act, files=["**/locales/**", "*.po"])
        self.assertEqual(crew.check(dict(r, activities=[good]))[0], [])

    # a skill the crew names that this Mac has not installed for an agent is a warning with its install group and
    # command (selfcheck D8); without agents, check() reads no machine state, so load() stays the same everywhere
    def test_a_named_skill_that_is_not_installed_is_a_warning_naming_its_group(self):
        r = roster(member(skills=["testing", "terraform-style-guide"]))
        with tempfile.TemporaryDirectory() as d:
            home, skills = Path(d) / "agent", Path(d) / "skills"
            (skills / "testing").mkdir(parents=True)
            (skills / "testing" / "SKILL.md").write_text("x")
            with mock.patch.dict(selfcheck.AGENT_HOMES, {"claude": (home, skills)}):
                _, warnings = crew.check(r, agents=("claude",))
                _, plain = crew.check(r)
        missing = [w for w in warnings if "not installed" in w]
        self.assertEqual(len(missing), 1, warnings)
        self.assertIn("claude: named by the crew but not installed: terraform-style-guide (group iac)", missing[0])
        self.assertIn("dev install --only skills", missing[0])
        self.assertFalse([w for w in plain if "not installed" in w])

    def test_member_lookup(self):
        for q, want in (("neo", "neo"), ("NEO", "neo"), ("devkit:neo", "neo"), ("The Oracle", "oracle"),
                        ("oracle", "oracle"), ("Oracle", "oracle"), ("the  oracle", "oracle"), ("Agent Smith", "smith"),
                        ("scout", "sparks"), ("devkit:scout", "sparks"), ("Keymaker", "keymaker")):
            with self.subTest(q=q):
                self.assertEqual(crew.member(q)["id"], want)
        for q in ("nobody", "", None, "Neo: build the parser"):
            with self.subTest(q=q):
                self.assertIsNone(crew.member(q))
        # progress names a subagent's lane through member(): by agent type, or by "Name: task" in its description
        self.assertEqual(pg.persona("devkit:neo", ""), "Neo")
        self.assertEqual(pg.persona("", "Oracle: acceptance criteria"), "The Oracle")
        self.assertEqual(pg.persona("devkit:morpheus", ""), "Morpheus")  # the roster has him; he is no subagent
        self.assertEqual(crew._for_label("Seraph: review the parser")["id"], "seraph")


# --------------------------------------------------------------------------- rendering

class RenderTests(unittest.TestCase):
    TOOLS = {"all": None, "build": "Read, Write, Edit, Grep, Glob, Bash, Skill", "review": "Read, Grep, Glob, Bash, Skill",
             "read": "Read, Grep, Glob, Skill"}

    def test_frontmatter_for_every_tools_and_tier_combination(self):
        for tools, allowed in self.TOOLS.items():
            for tier, model, effort in (("strong", "inherit", "high"), ("standard", "inherit", None),
                                        ("fast", "haiku", "low")):
                with self.subTest(tools=tools, tier=tier):
                    m = member(tools=tools, tier=tier, not_for=["design"])
                    fm = frontmatter(crew.render_claude(m, roster(m)))
                    self.assertEqual(fm["name"], "pat")
                    self.assertEqual(json.loads(fm["description"]), "Pat, test engineer. Writes the tests that "
                                     "matter. Use when: missing tests. Not for: design.")
                    self.assertEqual(fm.get("tools"), allowed)
                    self.assertEqual(fm["model"], model)
                    self.assertEqual(fm.get("effort"), effort)
                    self.assertEqual(fm.get("omitClaudeMd"), "true" if tier == "fast" else None)

    def test_body_sections_report_block_and_rules(self):
        m = member(personality="Curious and exact.", not_for=["design"])
        text = crew.render_claude(m, roster(m, rule="Write in the developer's language."))
        body = text.split("\n---\n", 1)[1]
        self.assertTrue(crew.is_generated(text))
        self.assertEqual(body.splitlines()[0], crew.MARK)
        self.assertTrue(body.splitlines()[1].startswith("You are Pat, the crew's test engineer. Curious and exact."))
        heads = [ln for ln in body.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads, ["## Mission", "## Goal", "## When the tech lead sends work to you", "## Skills to load first",
                                 "## Your procedure", "## Your place in the crew's RACI", "## Your devkit commands",
                                 "## How you work with the crew", "## Your report", "## Language"])
        self.assertIn("**[Firefly> DevKit · Test engineer (Pat)]** <project> · `<branch>` · <task>\n- **Done:**", body)
        self.assertIn("- **Check:** <✓ | ⚠ | ✗>", body)
        self.assertIn("Then deliver: the tests and their results.", body)
        self.assertIn('`Needs from the tech lead:`', body)
        self.assertIn("Not yours: design.", body)
        self.assertIn("\n`testing`\n", body)  # the skills on one line
        self.assertIn('dev crew ask "<question>"', body)
        self.assertIn('dev progress step "<what finished>" --check "<evidence>"', body)
        # members may launch narrow helpers within their own limits (Tank's F3: nested agents)
        self.assertIn("Launch a helper agent only for a narrow lookup or a slice of your own files", body)
        self.assertIn("cannot go deeper than progress.max_depth", body)
        self.assertNotIn("You cannot launch other agents", body)
        self.assertTrue(body.rstrip().endswith("Write in the developer's language."))
        review = crew.body(member(tools="review"), roster())
        self.assertIn("review-only", review)
        self.assertIn("never edit", review)
        read = crew.body(member(tools="read", skills=[]), roster())
        self.assertIn("You have no shell", read)
        self.assertIn("never edit", read)
        self.assertIn("None. Load a skill only when your brief names one.", read)
        self.assertNotIn("review-only", crew.body(member(tools="build"), roster()))

    # members record where a lesson applies (its anchors), so it is recalled by behavior in any language; a member
    # without a shell names them for Morpheus
    def test_the_learn_text_asks_for_the_lessons_anchors(self):
        shell = crew.body(member(), roster())
        for needle in ('dev lesson add "…" --cause "…" --prevent "…" --file', "--command", "--error",
                       'dev lesson anchor <id>'):
            self.assertIn(needle, shell)
        read = crew.body(member(tools="read", skills=[]), roster())
        self.assertIn("the files, commands or error text it applies to", read)

    def test_every_shipped_agent_is_short_and_its_report_passes_the_crew_canary(self):
        r = crew.load()
        for m in crew.members(r, include_main=False):
            with self.subTest(member=m["id"]):
                text = crew.render_claude(m, r)
                # a full definition (goal, procedure, RACI, commands, done-when), still about 2k tokens at most
                self.assertLess(len(text.splitlines()), 100)
                self.assertLess(len(text), 8000)
                self.assertTrue(canary.MARKER_RE.match(crew.status_header(m, "api", "main", "parser")))

    def test_the_main_session_role_gets_no_agent_and_scout_is_an_alias(self):
        r = crew.load()
        with self.assertRaises(ValueError):
            crew.render_claude(crew.member("morpheus"), r)
        with self.assertRaises(ValueError):
            crew.codex_role(crew.member("morpheus"), r)
        files = crew.agent_files(r)
        self.assertNotIn("morpheus.md", files)
        self.assertEqual(len(files), 26)  # 25 subagents + the scout alias
        scout = files["scout.md"]
        self.assertEqual(frontmatter(scout)["name"], "scout")
        self.assertIn("You are Sparks (alias: scout)", scout)
        self.assertEqual(scout.split("## Mission")[1], files["sparks.md"].split("## Mission")[1])


class CodexTests(unittest.TestCase):
    CODEX = {"profiles": {"fast": {"model": "luna", "model_reasoning_effort": "low"},
                          "deep": {"model": "astra", "model_reasoning_effort": "xhigh"}}}

    def test_tier_and_tools_mapping(self):
        for tier, want in (("fast", {"model": "luna", "model_reasoning_effort": "low"}),
                           ("strong", {"model": "astra", "model_reasoning_effort": "high"}), ("standard", {})):
            # review members keep a shell (tests, scans, dev progress); devkit's guard refuses their edits
            for tools, sandbox in (("all", None), ("build", None), ("review", None), ("read", "read-only")):
                with self.subTest(tier=tier, tools=tools):
                    m = member(tier=tier, tools=tools)
                    role = crew.codex_role(m, roster(m), self.CODEX)
                    self.assertEqual(list(role)[:3], ["name", "description", "developer_instructions"])
                    self.assertEqual(role["name"], "pat")
                    self.assertEqual(role["description"], crew.description(m))
                    self.assertEqual(role["developer_instructions"], crew.body(m, roster(m)))
                    self.assertEqual({k: role[k] for k in ("model", "model_reasoning_effort") if k in role}, want)
                    self.assertEqual(role.get("sandbox_mode"), sandbox)

    def test_roles_for_the_shipped_roster_are_valid_toml(self):
        from devkit import setup  # the writer that turns these dicts into ~/.codex/agents/<name>.toml
        roles = crew.codex_roles()
        self.assertNotIn("morpheus", roles)
        self.assertEqual(roles["scout"]["name"], "scout")
        self.assertEqual(roles["sparks"]["sandbox_mode"], "read-only")
        deep = core.read_json(crew.CODEX_CONF, {})["profiles"]["deep"]["model"]
        self.assertEqual((roles["seraph"]["model"], roles["seraph"]["model_reasoning_effort"]), (deep, "high"))
        self.assertNotIn("model", roles["neo"])
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        for name, role in roles.items():
            with self.subTest(role=name):
                text = "".join(f"{k} = {setup._toml_value(v)}\n" for k, v in role.items())
                self.assertEqual(tomllib.loads(text), role)


# --------------------------------------------------------------------------- sync

class ShippedProcedureTests(unittest.TestCase):
    # Morpheus tags each step's kind of work, follows the session graph and reopens a work item that comes back;
    # Seraph reads the graph before the diff (R2-11)
    def test_morpheus_and_seraph_name_todays_crew_commands(self):
        r = crew.load(team=False)

        def commands(mid):
            m = crew.member(mid, r)
            return set(m["devkit"]) | {c for x in m["procedure"] for c in x.get("devkit") or []}
        self.assertLessEqual({"dev crew graph", "dev task reopen", "dev progress assign --activity"},
                             commands("morpheus"))
        plan = next(x for x in crew.member("morpheus", r)["procedure"] if x["phase"] == "Plan")
        self.assertIn("`[backend]`", plan["do"])
        self.assertIn("dev crew graph", commands("seraph"))

    # the proofs devkit looks for are the commands the gates name: new tests that can fail, the local CI run, the
    # revised plan, and a recorded verdict (R2-6)
    def test_the_proofs_are_the_commands_the_gates_name(self):
        r = crew.load(team=False)
        for mid in ("neo", "trinity", "tank", "dozer", "switch", "rama", "kid", "trainman"):
            with self.subTest(member=mid):
                self.assertIn("dev test prove", crew.proofs(crew.member(mid, r)))
        self.assertIn("dev ci --run", crew.proofs(crew.member("link", r)))
        self.assertIn("dev ci --run", crew.proofs(crew.member("trainman", r)))
        self.assertIn("dev plan revise", crew.proofs(crew.member("architect", r)))
        self.assertIn("dev progress step", crew.proofs(crew.member("seraph", r)))


class EnforcementMatrixTests(unittest.TestCase):
    # every rule has a stable id (E-…) that docs cite (D11) and that survives a reworded rule
    def test_every_rule_has_a_unique_stable_id(self):
        ids = [row[0] for row in crew.ENFORCEMENT]
        self.assertTrue(all(len(row) == 5 for row in crew.ENFORCEMENT))
        self.assertEqual([i for i in ids if not selfcheck.MATRIX_ID.fullmatch(f"`{i}`")], [])
        self.assertEqual(len(ids), len(set(ids)))

    # M332: the rules 3fb1dbb (F5, F7, F8) and 5b7a6a3 (F6) wired are enforced, each by the check its row names; the
    # shell scope holds for review-only and read-only members only, and the row says so (D11 checks docs/crew.md)
    def test_the_wired_rules_are_enforced_by_their_checks(self):
        rows = {row[0]: row for row in crew.ENFORCEMENT}
        wired = ("E-irreversible", "E-verdict", "E-no-self-approval", "E-evidence", "E-escape", "E-shell-scope")
        self.assertEqual([rows[i][4] for i in wired], ["enforced"] * len(wired))
        self.assertEqual([r for r, *_, ok in crew.enforcement() if not ok], [])
        self.assertIn("review-only and read-only", rows["E-shell-scope"][1])

    # dev crew rules prints each rule with its id
    def test_rules_prints_each_rules_id(self):
        with TempState():
            rc, out, _ = run_cli("crew", "rules")
        self.assertEqual(rc, 0)
        for rid, rule, *_ in crew.ENFORCEMENT:
            with self.subTest(rule=rid):
                self.assertIn(rid, out)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.r = roster(member(), member(id="sparks", name="Sparks", tools="read", tier="fast", skills=[]))

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, changes):
        return sorted((c.split()[0], Path(c.split()[1]).name) for c in changes)

    def test_sync_is_idempotent_and_check_reports_drift(self):
        self.assertEqual(self.names(crew.sync(roster=self.r, agents_dir=self.dir)),
                         [("new", "pat.md"), ("new", "scout.md"), ("new", "sparks.md")])
        self.assertEqual(crew.sync(roster=self.r, agents_dir=self.dir), [])
        self.assertEqual(crew.sync(check=True, roster=self.r, agents_dir=self.dir), [])
        (self.dir / "pat.md").write_text("edited by hand\n")
        self.assertEqual(self.names(crew.sync(check=True, roster=self.r, agents_dir=self.dir)), [("update", "pat.md")])
        self.assertEqual((self.dir / "pat.md").read_text(), "edited by hand\n")  # --check changes nothing
        crew.sync(roster=self.r, agents_dir=self.dir)
        self.assertEqual((self.dir / "pat.md").read_text(), crew.render_claude(self.r["members"][0], self.r))

    def test_only_marked_files_are_removed(self):
        crew.sync(roster=self.r, agents_dir=self.dir)
        gone = dict(self.r, members=[self.r["members"][1]],  # Pat leaves the roster, and its kind of work
                    activities=[x for x in self.r["activities"] if x["R"] != ["pat"]])
        (self.dir / "mine.md").write_text("---\nname: mine\n---\nHand-written.\n")
        (self.dir / "quoted.md").write_text(f"---\nname: quoted\n---\nNotes.\n{crew.MARK}\n")  # mark, not first
        self.assertEqual(self.names(crew.sync(check=True, roster=gone, agents_dir=self.dir)), [("remove", "pat.md")])
        self.assertTrue((self.dir / "pat.md").exists())
        self.assertEqual(self.names(crew.sync(roster=gone, agents_dir=self.dir)), [("remove", "pat.md")])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["mine.md", "quoted.md", "scout.md", "sparks.md"])

    def test_shipped_agent_files_match_the_roster(self):
        self.assertEqual(crew.sync(check=True), [], "run: dev crew sync")


# --------------------------------------------------------------------------- briefs

class BriefTests(unittest.TestCase):
    def test_brief_has_scope_skills_return_report_format_commands_and_spawn_lines(self):
        neo = crew.member("neo")
        with tempfile.TemporaryDirectory() as d:  # not a git repository: project and branch stay placeholders
            text = ui.ANSI_RE.sub("", crew.brief_text(neo, "Build the CSV parser.", "lib/parser/**, tests/test_parser.py",
                                                      "2", cwd=d))
        for needle in ("subagent_type: devkit:neo", "agent type: neo", 'description: "Neo: ',
                       "You own only these files: `lib/parser/**`, `tests/test_parser.py`", "## Task\n\nBuild the CSV parser.",
                       "**[Firefly> DevKit · Senior engineer (Neo)]** <project> · `<branch>` · <the task, in a few words>",
                       "- **Check:** <✓ | ⚠ | ✗>", '`Needs from the tech lead:`', _sentence(neo["returns"]),
                       'dev progress step "<what finished>" --check "<evidence>"', 'dev crew ask "'):
            self.assertIn(needle, text)
        for s in neo["skills"]:
            self.assertIn(f"`{s}`", text)
        assign = next(ln.strip() for ln in text.splitlines() if "dev progress assign" in ln)
        self.assertIn('dev progress assign 2 --to "Neo" --scope "lib/parser/**,tests/test_parser.py"', assign)
        argv = shlex.split(assign.split("Link the lane ", 1)[1].split("   (")[0])[1:]
        a = cli.build_parser().parse_args(argv)  # the printed command is one the progress CLI accepts
        self.assertEqual((a.progress_cmd, a.which, a.to, a.scope), ("assign", "2", ["Neo"], "lib/parser/**,tests/test_parser.py"))

    def test_a_brief_marks_the_skills_the_crew_gates_check_apart_from_its_roles_list(self):
        # this run's briefs listed every skill alike, and some called a gated one optional; the gates check the
        # skills of each procedure phase the member works in (gates.gated_skills) and those of the files it changes
        with tempfile.TemporaryDirectory() as d:
            text = ui.ANSI_RE.sub("", crew.brief_text(crew.member("kid"), "Speed up the build.", "tools/**", cwd=d))
        part = text.split("## Skills to load first", 1)[1].split("\n## ", 1)[0]
        gated, also = part.split("Also listed for your role", 1)
        self.assertIn("The crew gates check these before you stop", gated)
        for skill, phases in (("superpowers:test-driven-development", "Build"), ("testing", "Build and Verify"),
                              ("docs", "Document"), ("superpowers:verification-before-completion", "Verify")):
            self.assertIn(f"- `{skill}` ({phases} phase", gated)
            self.assertNotIn(f"`{skill}`", also)
        self.assertIn("plus the skills devkit's skill rules require for the files you change", gated)
        for skill in ("workspace", "using-devkit", "communicating"):
            self.assertIn(f"- `{skill}`", also)
            self.assertNotIn(f"`{skill}`", gated)
        self.assertNotIn("optional", part.lower())

    def test_every_brief_names_the_member_s_own_scratch_folder(self):
        # the guard lets a member write only in scratchpad/<its id>/ of the session scratchpad; a brief that does not
        # say so sends members to the shared top level, where the guard refuses them (lesson L-fxs9q5evz83)
        with tempfile.TemporaryDirectory() as d:
            for mid, scope in (("neo", "lib/parser/**"), ("seraph", ""), ("smith", "")):
                with self.subTest(member=mid):
                    text = ui.ANSI_RE.sub("", crew.brief_text(crew.member(mid), "Fix it.", scope, cwd=d))
                    scope_part = text.split("## Scope", 1)[1].split("## Skills", 1)[0]
                    self.assertIn(f"`scratchpad/{mid}/`", scope_part)
                    for other in ("neo", "seraph", "smith"):
                        if other != mid:
                            self.assertNotIn(f"scratchpad/{other}/", scope_part)

    def test_every_brief_and_agent_file_has_the_member_end_its_background_work_before_it_reports(self):
        # Claude Code kept members listed for hours after their report while a background command they started ran
        rule = ("Before your final report, every background command you started has ended: keep working until it ends, "
                "or stop it.")
        r = crew.load()
        with tempfile.TemporaryDirectory() as d:
            for m in crew.members(r, include_main=False):
                with self.subTest(member=m["id"]):
                    text = ui.ANSI_RE.sub("", crew.brief_text(m, "Fix it.", "lib/**", cwd=d))
                    self.assertIn(rule, text.split("## Procedure", 1)[1].split("\n## ", 1)[0])
                    self.assertIn(rule, crew.body(m, r).split("## Your procedure", 1)[1].split("\n## ", 1)[0])

    def test_a_member_that_approves_several_kinds_of_work_reads_how_to_review_them_once(self):
        r = crew.load()
        body = crew.body(crew.member("seraph"), r)
        approves = [ln for ln in body.splitlines() if ln.startswith("- **You approve** ")]
        self.assertGreater(len(approves), 5)
        self.assertIn("- **You approve** developer tooling and experience done by DevEx engineer (Kid).", approves)
        self.assertEqual(body.count("approve it or send it back with the fixes"), 1)

    def test_brief_through_the_cli(self):
        rc, out, _ = run_cli("crew", "brief", "The Oracle", "Write", "acceptance", "criteria", "--step", "design")
        self.assertEqual(rc, 0)
        self.assertIn("subagent_type: devkit:oracle", out)
        self.assertIn('dev progress assign "design" --to "The Oracle"', out)
        self.assertNotIn("You have no shell", out)  # The Oracle is review: a shell to report, no edits
        rc, out, _ = run_cli("crew", "brief", "Sparks", "Find", "the", "parser")
        self.assertIn("You have no shell", out)  # a read-only member: Morpheus records for it
        self.assertEqual(run_cli("crew", "brief", "morpheus", "plan it")[0], 2)
        rc, _, err = run_cli("crew", "brief", "nobody", "x")
        self.assertEqual(rc, 2)
        self.assertIn("no crew member", err)

    def test_show(self):
        rc, out, _ = run_cli("crew", "show", "The", "Keymaker")
        self.assertEqual(rc, 0)
        for needle in ("Security engineer (The Keymaker)", "devkit:keymaker", "~/.codex/agents/keymaker.toml",
                       "github-actions-hardening", "build: read, write and run commands (no web)", "keywords"):
            self.assertIn(needle, out)
        self.assertIn("not a subagent", run_cli("crew", "show", "morpheus")[1])


def _sentence(s):
    return crew._sentence(s)


# --------------------------------------------------------------------------- live crew state, messages, decisions

class CrewSessionTests(unittest.TestCase):
    def test_ask_answer_and_inbox_through_the_cli(self):
        with TempState() as t:
            pg.start("Parser", plan=["parse", "review"])
            t.launch()
            rc, out, _ = run_cli("crew", "ask", "Should the parser accept empty lines?", from_agent="a1")
            self.assertEqual(rc, 0)
            self.assertIn("M1 to Tech lead (Morpheus): Should the parser accept empty lines?", out)
            rc, out, _ = run_cli("crew", "inbox")
            self.assertIn("M1 from Senior engineer (Neo): Should the parser accept empty lines?", out)
            self.assertIn("no new messages", run_cli("crew", "inbox")[1])  # delivered once
            rc, out, _ = run_cli("crew", "answer", "M1", "Yes,", "skip", "them")
            self.assertEqual(rc, 0)
            self.assertIn("M2 to Senior engineer (Neo), the answer to M1: Yes, skip them", out)
            self.assertIn("no new messages", run_cli("crew", "inbox")[1])  # the answer is Neo's, not Morpheus's
            self.assertIn("M2 from Tech lead (Morpheus): Yes, skip them (answer to M1)",
                          run_cli("crew", "inbox", from_agent="a1")[1])
            rc, _, err = run_cli("crew", "answer", "M9", "x")
            self.assertEqual(rc, 2)
            self.assertIn("no message M9", err)

    def test_ask_without_a_run_explains_and_exits_2(self):
        with TempState():
            rc, _, err = run_cli("crew", "ask", "anyone?")
            self.assertEqual(rc, 2)
            self.assertIn("no progress run", err)
            self.assertIn("no new messages", run_cli("crew", "inbox")[1])

    def test_roster_view_shows_lanes_assignments_open_messages_and_mode(self):
        with TempState() as t:
            pg.start("Parser", plan=["parse", "review"])
            pg.assign("review", "Seraph", scope="lib/**")
            t.launch()
            pg.crew_ask("Empty lines?", from_agent="a1")
            rc, out, _ = run_cli("crew")
            self.assertEqual(rc, 0)
            for needle in ("Crew · 26 members · Parser", "Working now", "build the parser", "Assigned steps",
                           "2. review → Code reviewer (Seraph) · pending · waiting for its agent · scope lib/**",
                           "Open messages", "M1 Senior engineer (Neo) → Tech lead (Morpheus) · not delivered: Empty lines?",
                           "Mode guided"):
                self.assertIn(needle, out)
            self.assertIn("● Senior engineer", out)  # the roster marks who is working
            for argv in (("crew", "--json"), ("crew", "--json", "--latest")):
                with self.subTest(argv=argv):
                    data = json.loads(run_cli(*argv)[1])
                    self.assertEqual(len(data["members"]), 26)
                    self.assertEqual(data["members"][1]["spawn"], {"claude_code": "devkit:hamann", "codex": "hamann"})
                    run = data["run"]
                    self.assertEqual((run["mode"], run["approved"], run["decisions"]), ("guided", [], []))
                    self.assertEqual([(x["member"], x["state"]) for x in run["lanes"]], [("Neo", "running")])
                    self.assertEqual([(x["member"], x["scope"]) for x in run["assigned"]], [("Seraph", ["lib/**"])])
                    self.assertEqual([(x["id"], x["from_name"], x["delivered"]) for x in run["messages"]],
                                     [("M1", "Neo", False)])
            pg.step("done", kind="done", force=True)
            self.assertIsNone(json.loads(run_cli("crew", "--json", "--latest")[1])["run"])  # no running run now

    def test_mode_guided_and_auto_with_approvals(self):
        with TempState():
            rc, out, _ = run_cli("crew", "mode")
            self.assertEqual(rc, 0)
            self.assertIn("Mode guided", out)
            self.assertIn("no progress run", out)
            self.assertEqual(run_cli("crew", "mode", "auto")[0], 2)  # no run to set it on
            pg.start("Parser", plan=["parse"])
            rc, out, _ = run_cli("crew", "mode", "auto", "--approve", "push feat/*, pr feat/*")
            self.assertEqual(rc, 0)
            self.assertIn("Mode auto", out)
            self.assertIn("no grant of the developer for push feat/*, pr feat/*: they open nothing", out)  # spec C5
            self.assertIn("pre-approved irreversible actions: push feat/*, pr feat/*", run_cli("crew", "mode")[1])
            rc, _, err = run_cli("crew", "mode", "auto", "--approve", "push")
            self.assertEqual(rc, 2)  # a pre-approval names its target (spec C6)
            self.assertIn("name one target for each pre-approval", err)
            rc, _, err = run_cli("crew", "mode", "auto", "--approve", "launch-rockets")
            self.assertEqual(rc, 2)
            self.assertIn("unknown action", err)
            rc, _, err = run_cli("crew", "mode", "guided", from_agent="a1")
            self.assertEqual(rc, 2)  # a crew member never changes the mode
            self.assertIn("only the tech lead", err)
            self.assertEqual(pg.mode_of(pg.load_run("claude", "s1")), "auto")
            self.assertEqual(run_cli("crew", "mode", "--approve", "push")[0], 2)

    def test_auto_mode_goes_ahead_and_the_developer_can_override(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            pg.set_mode("auto")
            rc, out, _ = run_cli("crew", "ask", "--decision", "Rewrite or patch the parser?",
                                 "--options", "A: rewrite it | B: patch it", "--recommend", "A")
            self.assertEqual(rc, 0)
            self.assertIn("auto mode: go ahead with A: rewrite it (D1, recorded; the developer can revisit it with "
                          "dev crew decisions)", out)
            rc, out, _ = run_cli("crew", "decisions")
            self.assertIn("auto · review", out)
            self.assertIn("no pending decisions", run_cli("crew", "decisions", "--pending")[1])
            rc, out, _ = run_cli("crew", "decide", "D1", "B")
            self.assertEqual(rc, 0)  # no answer of the developer to D1 reached devkit: Morpheus's, unverified
            self.assertIn("D1 recorded by the tech lead, not verified by the developer: B: patch it (overrides the "
                          "automatic choice A)", out)
            listed = run_cli("crew", "decisions")[1]
            self.assertIn("B: patch it (was A)", listed)
            self.assertIn("morpheus · unverified", listed)

    def test_withdraw_closes_a_decision_for_the_tech_lead_only(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            run_cli("crew", "ask", "--decision", "Rewrite or patch the parser?", "--options",
                    "A: rewrite it | B: patch it", "--recommend", "A")
            rc, _, err = run_cli("crew", "withdraw", "D1", "--why", "not mine to drop", from_agent="a1")
            self.assertEqual(rc, 2)
            self.assertIn("only the tech lead", err)
            rc, out, _ = run_cli("crew", "withdraw", "D1", "--why", "the parser stays as it is")
            self.assertEqual(rc, 0)
            self.assertIn("D1 withdrawn: the parser stays as it is", out)
            self.assertIn("no pending decisions", run_cli("crew", "decisions", "--pending")[1])

    def test_a_delivered_notice_is_no_open_message(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            pg.crew_ask("CI passed on feat/parser", to="main", from_agent="ci")
            run_cli("crew", "inbox")  # delivered to the tech lead
            self.assertEqual(crew.open_messages(pg.load_run("claude", "s1")), [])

    def test_decisions_marks_a_moot_decision_and_pending_leaves_it_out(self):
        with TempState() as t:
            pg.start("Parser", plan=["parse"])
            t.launch()
            run_cli("crew", "ask", "--decision", "Skip empty lines?", "--options", "A: skip | B: reject",
                    "--recommend", "A", from_agent="a1")
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo",
                                      "last_assistant_message": "stopped"}, False)  # the asking lane ends
            self.assertIn("no pending decisions", run_cli("crew", "decisions", "--pending")[1])
            self.assertIn("moot", run_cli("crew", "decisions")[1])

    def test_dev_status_lists_only_the_decisions_that_still_need_the_developer(self):
        from devkit import registry
        now = time.time()
        run = {"v": 2, "project": "acme/api", "goal": "Release", "state": "running", "started": now - 3600,
               "updates": [], "intervals": [], "messages": [], "steps": 1, "step": 0,
               "plan": [{"name": "api", "state": "pending", "owner": "main", "est": 10}],
               "agents": {"lane1": {"label": "Tank: api", "persona": "Tank", "state": "done", "started": now - 1800,
                                    "finished": now - 600, "updates": []}},
               "decisions": [{"id": "D1", "ts": now - 900, "state": "pending", "from": "lane1", "from_name": "Tank",
                              "question": "Keep the old endpoint?", "options": [], "recommend": ""},
                             {"id": "D2", "ts": now - 300, "state": "pending", "from": "main",
                              "from_name": "Morpheus", "question": "Ship on Friday?", "options": [], "recommend": ""}]}
        p = {"id": "acme/api", "owner": "acme", "name": "api", "path": None, "local": False, "main": {},
             "worktrees": [], "canonical_ok": True, "prs_open": [], "status_line": {"line": ""}}
        with mock.patch.object(registry, "alias_map", lambda projects: {}):
            v = cli.status_view({"projects": [p], "issues": [], "sessions": []}, p, None, runs=[run],
                                ci={"runs": []}, roster=crew.load(team=False), now=now)
        self.assertEqual([n["id"] for n in v["needs_you"] if n["kind"] == "decision"], ["D2"])

    def test_a_decision_names_the_steps_the_developers_answer_approves(self):
        # M280 (F6, D4): dev crew ask --decision --approves "<steps>" keeps them on the decision, checked against the plan
        with TempState():
            pg.start("Parser", plan=["parse", "review"])
            rc, _, _ = run_cli("crew", "ask", "--decision", "Ship the parser as it is?", "--options",
                               "A: yes | B: no", "--recommend", "A", "--approves", "parse")
            self.assertEqual(rc, 0)
            self.assertEqual(pg.load_run("claude", "s1")["decisions"][0]["approves"], ["parse"])
            rc, _, err = run_cli("crew", "ask", "--decision", "And the docs?", "--options", "A: yes | B: no",
                                 "--recommend", "A", "--approves", "nosuch")
            self.assertEqual(rc, 2)
            self.assertIn("nosuch", err)

    def test_decisions_ask_prints_the_question_with_the_recommended_option_first(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            run_cli("crew", "ask", "--decision", "Rewrite or patch the parser?", "--options",
                    "A: rewrite it | B: patch it", "--recommend", "B")
            rc, out, _ = run_cli("crew", "decisions", "--ask", "D1")
            self.assertEqual(rc, 0)
            self.assertEqual(pg.load_run("claude", "s1")["decisions"][0]["target"], "")
            self.assertIn("D1 · Rewrite or patch the parser?", out)
            self.assertLess(out.index("B: patch it (Recommended)"), out.index("A: rewrite it"))
            self.assertIn("approve D1: <option>", out)
            self.assertEqual(run_cli("crew", "decisions", "--ask", "D9")[0], 2)
            rc, _, err = run_cli("crew", "ask", "--decision", "Release 26.11.4 now?", "--options", "A: yes | B: no",
                                 "--recommend", "A", "--irreversible", "release")
            rc, _, err = run_cli("crew", "decide", "D2", "yes")
            self.assertEqual(rc, 2)  # irreversible, and no answer of the developer: it stays pending
            self.assertIn("D2 · Release 26.11.4 now?", err)
            self.assertEqual(pg.load_run("claude", "s1")["decisions"][1]["state"], "pending")

    def test_decisions_ask_json_batches_four_questions_per_call_with_their_context(self):
        with TempState() as t:
            pg.start("Parser", plan=["parse", "ship"])
            pg.set_mode("guided")
            pg.assign("parse", "Neo")
            t.launch()
            run_cli("crew", "ask", "--decision", "Skip empty lines or reject the file?", "--options",
                    "A: skip | B: reject", "--recommend", "B", from_agent="a1")
            for n in range(2, 6):
                run_cli("crew", "ask", "--decision", f"Keep option {n}?", "--options", "A: keep | B: drop",
                        "--recommend", "A")
            rc, out, _ = run_cli("crew", "decisions", "--ask", "--json")
            self.assertEqual(rc, 0)
            calls = json.loads(out)["calls"]
            self.assertEqual([[q["header"] for q in c["questions"]] for c in calls], [["D1", "D2", "D3", "D4"], ["D5"]])
            q = calls[0]["questions"][0]
            self.assertTrue(q["question"].startswith("D1 · Skip empty lines or reject the file? "), q["question"])
            self.assertIn("Senior engineer (Neo) asks", q["question"])
            self.assertIn("“parse”", q["question"])  # the step it blocks
            self.assertEqual([o["label"] for o in q["options"]], ["B: reject (Recommended)", "A: skip"])
            self.assertIs(q["multiSelect"], False)
            rc, out, _ = run_cli("crew", "decisions", "--ask", "D5", "d2", "--json")
            self.assertEqual([q["header"] for q in json.loads(out)["calls"][0]["questions"]], ["D2", "D5"])

    def test_decisions_ask_puts_each_irreversible_decision_in_a_call_of_its_own(self):
        # the Code reviewer's finding 3: another question in the call could redefine an irreversible one's options
        with TempState():
            pg.start("Parser", plan=["parse", "ship"])
            for n in range(1, 7):
                if n == 2:
                    run_cli("crew", "ask", "--decision", "Release v26.11.4 now?", "--options", "A: yes | B: no",
                            "--recommend", "A", "--irreversible", "release", "--target", "v26.11.4")
                elif n == 6:
                    run_cli("crew", "ask", "--decision", "Push fix/parser now?", "--irreversible", "push",
                            "--target", "fix/parser")
                else:
                    run_cli("crew", "ask", "--decision", f"Keep option {n}?", "--options", "A: keep | B: drop",
                            "--recommend", "A")
            rc, out, _ = run_cli("crew", "decisions", "--ask", "--json")
            self.assertEqual(rc, 0)
            self.assertEqual([[q["header"] for q in c["questions"]] for c in json.loads(out)["calls"]],
                             [["D1", "D3", "D4", "D5"], ["D2"], ["D6"]])
            rc, out, _ = run_cli("crew", "decisions", "--ask")
            self.assertIn("Ask the developer in 3 question calls, one after the other", out)

    def test_a_decision_without_two_to_four_lettered_options_and_a_recommendation_is_refused(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            for args, fix in (
                    (["Skip or reject?", "--options", "skip | reject", "--recommend", "skip"],
                     'dev crew ask --decision "Skip or reject?" --options "A: skip | B: reject" --recommend A'),
                    (["Skip them?", "--options", "A: skip", "--recommend", "A"], "2 to 4 options"),
                    (["Which?", "--options", "A: a | B: b | C: c | D: d | E: e", "--recommend", "A"], "2 to 4 options"),
                    (["Skip or reject?", "--options", "A: skip | B: reject"], "--recommend A"),
                    (["Skip or reject?", "--options", "A: skip | B: reject", "--recommend", "C"], "--recommend A"),
                    (["Which file? " + "x" * 300, "--options", "A: skip | B: reject", "--recommend", "A"], "300")):
                rc, _, err = run_cli("crew", "ask", "--decision", *args)
                self.assertEqual(rc, 2, args)
                self.assertIn(fix, err, args)
            self.assertEqual(pg.load_run("claude", "s1").get("decisions") or [], [])

    def test_an_irreversible_approval_without_options_is_asked_as_yes_or_no(self):
        with TempState():
            pg.start("Parser", plan=["parse"])
            rc, _, err = run_cli("crew", "ask", "--decision", "Push the fix branch?", "--irreversible", "push",
                                 "--target", "fix/parser")  # the question must name what the push acts on
            self.assertEqual(rc, 2)
            self.assertIn('--decision "Push the fix branch? (fix/parser)"', err)
            rc, _, _ = run_cli("crew", "ask", "--decision", "Push fix/parser now?", "--irreversible", "push",
                               "--target", "fix/parser")  # what the guard tells an agent to run
            self.assertEqual(rc, 0)
            d = pg.load_run("claude", "s1")["decisions"][0]
            self.assertEqual((d["options"], d["recommend"]), (["A: yes", "B: no"], "A"))

    def test_irreversible_or_guided_decisions_wait_for_the_developer(self):
        with TempState() as t:
            pg.start("Parser", plan=["parse", "ship"])
            pg.set_mode("auto", approve="push feat/*")
            rc, out, _ = run_cli("crew", "ask", "--decision", "Release 26.11.2 now?", "--recommend", "yes",
                                 "--irreversible", "release", "--target", "v26.11.2")
            self.assertEqual(rc, 0)  # release is not pre-approved: Morpheus asks the developer
            self.assertEqual(pg.load_run("claude", "s1")["decisions"][0]["target"], "v26.11.2")
            self.assertIn("ask the developer now in one question call (dev crew decisions --ask prints it), then: "
                          'dev crew decide D1 "<answer>"', out)
            pg.set_mode("guided")
            pg.assign("parse", "Neo")
            t.launch()
            rc, out, _ = run_cli("crew", "ask", "--decision", "Drop the legacy column?", "--options", "A: drop | B: keep",
                                 "--recommend", "B", from_agent="a1")
            self.assertEqual(rc, 0)
            self.assertIn("waiting for the developer's decision D2: stop here and end your report with your status "
                          "block (Check: – waiting for D2; Needs from the tech lead: D2). The tech lead resumes you "
                          "with the answer", out)
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["agents"]["a1"].get("waiting"), "D2")
            view = run_cli("crew")[1]
            self.assertIn("Decisions waiting for the developer", view)
            self.assertIn("D2 Senior engineer (Neo): Drop the legacy column? · options A: drop | B: keep · recommends B",
                          view)
            self.assertEqual([d["id"] for d in json.loads(run_cli("crew", "--json")[1])["run"]["decisions"]],
                             ["D1", "D2"])
            rc, _, err = run_cli("crew", "decide", "D2", "B", from_agent="a1")
            self.assertEqual(rc, 2)  # only Morpheus records the developer's answers
            self.assertIn("only the tech lead", err)
            rc, out, _ = run_cli("crew", "decide", "D2", "B")
            self.assertEqual(rc, 0)
            self.assertIn("resume Senior engineer (Neo) with SendMessage / send_input and the answer", out)
            self.assertNotIn("waiting", pg.load_run("claude", "s1")["agents"]["a1"])
            self.assertIn("Drop the legacy column? → B", run_cli("crew", "inbox", from_agent="a1")[1])
            self.assertEqual(run_cli("crew", "decide", "D2", "A")[0], 2)  # the developer's answer stands
            self.assertEqual(run_cli("crew", "ask", "--decision", "x", "--to", "Seraph")[0], 2)


class DispatchCliTests(unittest.TestCase):
    """dev crew route, delegate, decline and next (dispatch.py does the routing; these check the commands)."""

    def test_route_ranks_with_reasons_and_prints_a_launch_line_that_parses(self):
        rc, out, _ = run_cli("crew", "route", "add a database migration for the invoices table",
                             "--files", "db/migration/V3__invoices.sql")
        self.assertEqual(rc, 0)
        self.assertIn("Dozer", out)
        self.assertIn("file db/migratio", out)  # the file weighs in, and the reason says so
        launch = next(ln.strip() for ln in out.splitlines() if ln.strip().startswith("launch: "))
        self.assertEqual(launch, 'launch: dev crew brief dozer "add a database migration for the invoices table"')
        a = cli.build_parser().parse_args(shlex.split(launch[len("launch: dev "):]))
        self.assertEqual((a.crew_cmd, a.name, a.task), ("brief", "dozer", ["add a database migration for the invoices table"]))
        for argv, needle in ((("crew", "route"), "say what the task is"), (("crew", "route", "--apply"), "goes with --plan"),
                             (("crew", "route", "x", "--plan"), "drop the task")):
            with self.subTest(argv=argv):
                rc, _, err = run_cli(*argv)
                self.assertEqual(rc, 2)
                self.assertIn(needle, err)

    def test_the_slot_total_is_the_parallel_limit_that_drops_to_one_on_a_short_quota(self):
        from unittest import mock
        with TempState():
            run = {"v": 2, "agent": "claude", "session": "s1", "plan": [], "agents": {}}
            with mock.patch.object(pg, "quota_used", lambda agent, now=None: 95.0):  # the 5-hour usage: external
                self.assertEqual(crew.ready_view(run)["max_parallel"], 1)
            with mock.patch.object(pg, "quota_used", lambda agent, now=None: 10.0):
                self.assertEqual(crew.ready_view(run)["max_parallel"], pg.cfg()["max_parallel"])

    def test_route_the_plan_then_apply(self):
        with TempState():
            rc, _, err = run_cli("crew", "route", "--plan")
            self.assertEqual(rc, 2)
            self.assertIn("no progress run", err)
            pg.start("Feature", plan=["requirements and acceptance criteria", "database migration", "pipeline caching"])
            rc, out, _ = run_cli("crew", "route", "--plan")
            self.assertEqual(rc, 0)
            self.assertIn("requirements and acceptance criteria → Business analyst (The Oracle)", out)
            self.assertIn("database migration → Database engineer (Dozer)", out)
            self.assertIn("assign them: dev crew route --plan --apply", out)
            self.assertFalse(any(s.get("label") for s in pg.load_run("claude", "s1")["plan"]))  # only proposed
            rc, out, _ = run_cli("crew", "route", "--plan", "--apply")
            self.assertIn("assigned 3 step(s)", out)
            self.assertEqual([s["label"] for s in pg.load_run("claude", "s1")["plan"]], ["The Oracle", "Dozer", "Link"])
            self.assertIn("every open plan step already has a member", run_cli("crew", "route", "--plan")[1])

    def test_delegate_to_a_working_member_or_to_one_morpheus_launches(self):
        with TempState() as t:
            self.assertEqual(run_cli("crew", "delegate", "anything")[0], 2)  # no run yet
            pg.start("Feature", plan=["parser=30m"])
            t.launch("a1", "devkit:neo", "Neo: parser")
            t.launch("a2", "devkit:dozer", "Dozer: schema")
            rc, out, _ = run_cli("crew", "delegate", "add a nullable column for the export date", "--to", "Dozer",
                                 "--scope", "db/**", from_agent="a1")
            self.assertEqual(rc, 0)
            self.assertIn("step “add a nullable column for the export date” for Database engineer (Dozer) · scope db/**",
                          out)
            self.assertIn("in the Database engineer (Dozer)'s lane now (they got a message)", out)
            self.assertIn("Senior engineer (Neo) delegates", run_cli("crew", "inbox", from_agent="a2")[1])
            rc, out, _ = run_cli("crew", "delegate", "review the parser before merge", from_agent="a1")
            self.assertEqual(rc, 0)
            self.assertIn("for Code reviewer (Seraph) (routed: ", out)
            self.assertIn("The tech lead launches Code reviewer (Seraph) when it is ready: dev crew next", out)
            self.assertIn("crew.delegate", t.audit_actions())  # recorded, in the test's own audit trail
            for argv, needle in ((("--to", "Nobody"), "no crew member"), (("--after", "no-such-step"), "no step")):
                with self.subTest(argv=argv):
                    rc, _, err = run_cli("crew", "delegate", "something", *argv)
                    self.assertEqual(rc, 2)
                    self.assertIn(needle, err)

    def test_decline_hands_the_step_back_to_morpheus(self):
        with TempState() as t:
            pg.start("Feature", plan=["parser=30m"])
            t.launch("a1", "devkit:neo", "Neo: parser")
            t.launch("a2", "devkit:dozer", "Dozer: schema")
            run_cli("crew", "delegate", "add the export column", "--to", "Dozer")
            rc, _, err = run_cli("crew", "decline", "add the export column", "--why", "busy", from_agent="a1")
            self.assertEqual(rc, 2)  # Neo cannot hand back Dozer's step
            self.assertIn("is not yours", err)
            rc, _, err = run_cli("crew", "decline", "add the export column", from_agent="a2")
            self.assertEqual(rc, 2)  # a reason is required
            self.assertIn("say why", err)
            rc, out, _ = run_cli("crew", "decline", "add the export column", "--why", "it needs a migration plan first",
                                 from_agent="a2")
            self.assertEqual(rc, 0)
            self.assertIn("handed “add the export column” back to the tech lead: it needs a migration plan first", out)
            step = pg.load_run("claude", "s1")["plan"][-1]
            self.assertEqual((step["state"], step["owner"], step["label"]), ("pending", "main", ""))
            inbox = run_cli("crew", "inbox")[1]
            self.assertIn("from Database engineer (Dozer): ", inbox)
            self.assertIn(" declined “add the export column”", inbox)
            self.assertIn("crew.decline", t.audit_actions())

    def test_next_shows_ready_queued_waiting_busy_and_the_mode(self):
        with TempState() as t:
            self.assertIn("no progress run", run_cli("crew", "next")[1])
            pg.start("Feature", plan=["design=30m", "api=1h after design", "ui=1h after design", "docs=10m"])
            for step, who in (("api", "Tank"), ("ui", "Trinity"), ("docs", "Ghost")):
                pg.assign(step, who)
            rc, out, _ = run_cli("crew", "next")
            self.assertEqual(rc, 0)
            self.assertIn("Slots: 0 of 4 slots in use · 1 kept for the developer's own request · mode guided", out)
            self.assertIn("Ready to launch", out)
            self.assertIn('  Technical writer (Ghost): docs · ~10m, no step waits for it · waiting since', out)
            self.assertIn('\n    dev crew brief ghost docs --step docs', out)
            self.assertIn("Backend engineer (Tank): api · after design", out)
            self.assertIn("Frontend engineer (Trinity): ui · after design", out)
            self.assertNotIn("auto mode", out)
            self.assertEqual(run_cli(*shlex.split('crew brief ghost "docs" --step "docs"'))[0], 0)  # runs as printed
            pg.set_mode("auto")
            self.assertIn("auto mode: launch all ready members now, in one message", run_cli("crew", "next")[1])
            for i, who in enumerate(("neo", "mouse", "dozer", "link")):  # every crew slot busy
                t.launch(f"b{i}", f"devkit:{who}", f"{who.title()}: other work")
            rc, out, _ = run_cli("crew", "next")
            self.assertIn("Slots: 4 of 4 slots in use", out)
            self.assertIn("Queued until a slot frees", out)
            self.assertIn("  Technical writer (Ghost): docs · ~10m", out)
            self.assertIn("Busy", out)
            self.assertIn("QA engineer (Mouse) · other work", out)
            self.assertNotIn("Ready to launch", out)
            self.assertNotIn("launch all ready members", out)
            ready = json.loads(run_cli("crew", "--json")[1])["run"]["ready"]
            self.assertEqual((ready["free"], ready["max_parallel"], ready["launch"]), (0, 4, []))
            self.assertEqual([(q["member"], q["launch"]) for q in ready["queued"]],
                             [("Ghost", 'dev crew brief ghost docs --step docs')])
            self.assertEqual(sorted((w["member"], w["why"]) for w in ready["waiting"]),
                             [("Tank", "after design"), ("Trinity", "after design")])
            self.assertEqual(len(ready["busy"]), 4)


# --------------------------------------------------------------------------- audit, hooks, MCP

class WiringTests(unittest.TestCase):
    def test_audit_records_only_what_changes_state(self):
        for argv in (["crew"], ["crew", "--json", "--latest"], ["crew", "show", "neo"], ["crew", "brief", "neo", "x"],
                     ["crew", "inbox"], ["crew", "decisions", "--pending"], ["crew", "mode"], ["crew", "sync", "--check"],
                     ["crew", "route", "add a migration"], ["crew", "route", "--plan"], ["crew", "next"]):
            with self.subTest(argv=argv):
                self.assertFalse(audit.audited(argv))
        for argv in (["crew", "sync"], ["crew", "ask", "x"], ["crew", "answer", "M1", "x"], ["crew", "decide", "D1", "A"],
                     ["crew", "mode", "auto"], ["crew", "mode", "auto", "--approve", "push"],
                     ["crew", "route", "--plan", "--apply"], ["crew", "delegate", "add a column", "--to", "dozer"],
                     ["crew", "decline", "3", "--why", "needs a plan"]):
            with self.subTest(argv=argv):
                self.assertTrue(audit.audited(argv))

    def test_subagent_crew_commands_are_routed_to_their_lane(self):
        def route(cmd, aid="a1"):
            r = hooks._route_subagent_progress({"agent_id": aid, "tool_name": "Bash", "tool_input": {"command": cmd}})
            return r and r["command"]
        for cmd in ('dev crew ask "empty lines?"', "dev crew inbox", 'dev crew answer M1 "yes"', "dev crew decide D1 A",
                    "dev crew decisions", "dev crew mode auto", "~/bin/dev crew mode", 'dev progress step "x"',
                    'dev crew delegate "add a column" --to Dozer', 'dev crew decline 3 --why "needs a plan"',
                    'dev crew route "add a migration"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(route(cmd), f"DEVKIT_FROM_AGENT=a1 {cmd}")
        for cmd in ("dev crew", "dev crew sync", "dev crew show neo", "dev crew next", "dev crewmode",
                    "dev crew ask x && rm -rf build"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(route(cmd))
        self.assertIsNone(route("dev crew inbox", aid=None))  # the main session: untouched

    def test_devkit_crew_is_a_read_only_mcp_tool(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "devkit_crew")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tool["inputSchema"]["properties"], {})
        argv = mcp.build_argv("devkit_crew", mcp.validate("devkit_crew", {}))
        self.assertEqual(argv, ["crew", "--json", "--latest"])
        self.assertFalse(audit.audited(argv))
        a = cli.build_parser().parse_args(argv)
        self.assertEqual((a.cmd, a.json, a.latest, a.crew_cmd), ("crew", True, True, None))
        from devkit import desktop
        self.assertIn("devkit_crew", [t["name"] for t in desktop.source_manifest()["tools"]])

    def test_the_whole_roster_fits_one_mcp_result(self):
        with TempState():
            data = json.loads(run_cli("crew", "--json", "--latest")[1])
        self.assertIsNone(data["run"])
        digest = mcp._HANDLERS["devkit_crew"][3]  # what the tool returns: the digest, then the prune
        self.assertLess(len(mcp.compact_json(mcp._prune(digest(data, {})))), mcp.MAX_TEXT)  # never cut by mcp.cap()
        self.assertIn("raci", digest(data, {}))
        self.assertEqual(data["members"][0]["id"], "morpheus")
        self.assertNotIn("spawn", data["members"][0])  # the main session's role is never spawned


if __name__ == "__main__":
    unittest.main()
