# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The crew roster holds together: every kind of work in config/crew.json has exactly one approver and at least one
# member who does it, all crew members; every skill config/crew.json and config/skill-rules.json name is one devkit
# ships (plugin/skills) or installs (config/skills.json, the plugins of config/claude/plugins.json); every kind of work
# a skill rule names exists; and every member's procedure ends by handing its work on, naming only roles that exist.
# Then the procedures that put the root cause first, research right and requirements clear: the debugging kind of
# work, the phases that use the root-cause, research and functional-analysis skills, and the skill rules that require
# them. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_crew_roster
import contextlib
import copy
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import approvals, cli, crew, raci, skillguard  # noqa: E402

RULES = ROOT / "config" / "skill-rules.json"
# "the Frontend engineer (Trinity)": a role, then the member's name in parentheses (one or two capitalized words)
NAMED = re.compile(r"\(([A-Z][\w-]*(?: [A-Z][\w-]*)?)\)")
FIXERS = ("neo", "tank", "trinity", "kid", "link", "dozer", "switch", "niobe", "lock", "mouse")


def shipped():
    return json.loads(crew.ROSTER.read_text()), json.loads(RULES.read_text())["rules"]


def _phase_skills(m):
    return [(f"`{m.get('id')}` {ph.get('phase')}", s) for ph in m.get("procedure") or []
            for s in ph.get("skills") or []]


def _hands_off(m) -> bool:
    # the main session closes the run and writes the handoff note; a member with a shell records its step for the
    # tech lead; a member without one hands off through its report, and the tech lead records the step for it
    last = (m.get("procedure") or [{}])[-1]
    cmds = [" ".join(c.split()) for c in last.get("devkit") or []] + [" ".join(str(last.get("proof") or "").split())]
    if m.get("main"):
        return "dev handoff" in cmds
    return "dev progress step" in cmds or m.get("tools") not in ("all", "build", "review")


def problems(roster, rules, known=None) -> list:
    known = known or crew.known_skills()
    members = {m["id"]: m for m in roster.get("members") or []}
    by_name = {m["name"]: m for m in members.values()}
    out = []
    for a in roster.get("activities") or []:
        aid = a.get("id")
        if not isinstance(a.get("A"), str) or not a["A"]:
            out.append(f"kind of work `{aid}`: A must be exactly one member, not {a.get('A')!r}")
        elif a["A"] not in members and a["A"] != approvals.DEVELOPER:
            out.append(f"kind of work `{aid}`: A `{a['A']}` is not a crew member")
        if not a.get("R"):
            out.append(f"kind of work `{aid}`: nobody is Responsible")
        for k in ("R", "C", "I"):
            out += [f"kind of work `{aid}`: {k} `{x}` is not a crew member" for x in a.get(k) or [] if x not in members]
    named = [(f"`{m['id']}`", s) for m in members.values() for s in m.get("skills") or []]
    named += [x for m in members.values() for x in _phase_skills(m)]
    named += [(f"kind of work `{a.get('id')}`", s) for a in roster.get("activities") or []
              for s in (a.get("knowledge") or {}).get("skills") or []]
    named += [(f"skill rule `{r.get('id')}` ({agent})", s) for r in rules
              for agent, names in (r.get("skills") or {}).items() for s in names]
    out += [f"{where} names `{s}`, a skill devkit neither ships nor installs" for where, s in named
            if not crew.skill_known(s, known)]
    acts = {a.get("id") for a in roster.get("activities") or []}
    out += [f"skill rule `{r.get('id')}` names kind of work `{x}`, which the roster does not have" for r in rules
            for x in r.get("activities") or [] if x not in acts]
    for m in members.values():
        if not _hands_off(m):
            out.append(f"`{m['id']}`: the last phase, {m['procedure'][-1].get('phase')}, hands nothing on")
        last = m["procedure"][-1]
        text = f"{last.get('phase')} {last.get('do')}"
        for name in NAMED.findall(text):
            who = by_name.get(name)
            if not who:
                out.append(f"`{m['id']}` hands off to {name}, who is not a crew member")
            elif not re.search(re.escape(who["title"]) + r" \(" + re.escape(name) + r"\)", text, re.I):
                out.append(f"`{m['id']}` hands off to {name} without the role {who['title']}")
    return out


def _activity(roster, aid):
    return next(a for a in roster["activities"] if a["id"] == aid)


def _member(roster, mid):
    return next(m for m in roster["members"] if m["id"] == mid)


class RosterConsistencyTests(unittest.TestCase):
    # the shipped files hold together; then each check on a broken copy of them, from the debugging kind of work,
    # the analyst's hand-off and the new skill rules
    def test_the_shipped_files_hold_together_and_a_missing_member_or_skill_or_hand_off_is_found(self):
        roster, rules = shipped()
        self.assertEqual(problems(roster, rules), [])
        roster, rules = copy.deepcopy(roster), copy.deepcopy(rules)
        debug = _activity(roster, "debug")
        debug["R"], debug["A"], debug["C"] = ["nobody"], ["seraph", "neo"], debug["C"] + ["ghostwriter"]
        debug["knowledge"]["skills"].append("no-such-skill")
        _member(roster, "neo")["procedure"][0]["skills"].append("devkit:no-such-skill")
        rules.append({"id": "made-up", "activities": ["debugging"], "skills": {"claude": ["root-cause"],
                                                                               "codex": ["devkit-root-cause"]}})
        tank = _member(roster, "tank")
        tank["procedure"][-1]["devkit"], tank["procedure"][-1]["proof"] = ["dev test"], "dev test"
        oracle = _member(roster, "oracle")
        oracle["procedure"][-1]["do"] += " Then the Database admin (Dozer) and the QA engineer (Mousey)."
        found = "\n".join(problems(roster, rules))
        for want in ("kind of work `debug`: R `nobody` is not a crew member",
                     "kind of work `debug`: A must be exactly one member",
                     "kind of work `debug`: C `ghostwriter` is not a crew member",
                     "kind of work `debug` names `no-such-skill`",
                     "`neo` Reproduce names `devkit:no-such-skill`",
                     "skill rule `made-up` (codex) names `devkit-root-cause`",
                     "skill rule `made-up` names kind of work `debugging`",
                     "`tank`: the last phase, Hand off, hands nothing on",
                     "`oracle` hands off to Dozer without the role Database engineer",
                     "`oracle` hands off to Mousey, who is not a crew member"):
            with self.subTest(problem=want):
                self.assertIn(want, found)


class RootCauseFirstTests(unittest.TestCase):
    def test_debugging_is_neos_kind_of_work_and_the_code_reviewer_approves_it(self):
        r = crew.load(team=False)
        act = raci.activity("debug", r)
        self.assertEqual((act["R"], act["A"], act["C"], act["I"]), (["neo"], "seraph", ["mouse", "merovingian"],
                                                                    ["morpheus"]))
        self.assertEqual(act["knowledge"]["skills"], ["root-cause", "superpowers:systematic-debugging", "testing",
                                                      "learning-loop"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["crew", "raci", "debug"]), 0)
        self.assertIn("Senior engineer (Neo)", out.getvalue())
        self.assertIn("Code reviewer (Seraph)", out.getvalue())

    # the words are only the fallback guess, in English and in Spanish alike
    def test_a_debugging_task_reads_as_debugging_in_english_and_spanish(self):
        r = crew.load(team=False)
        for text in ("find the root cause of the crash in checkout", "encuentra la causa raíz del fallo en el pago"):
            with self.subTest(task=text):
                top = raci.best(text, r)
                self.assertIsNotNone(top, "no clear kind of work")
                self.assertEqual(top[0]["id"], "debug")

    def test_a_debugging_brief_lists_root_cause_first(self):
        r = crew.load(team=False)
        with tempfile.TemporaryDirectory() as d:
            text = crew.brief_text(crew.member("neo", r), "find why the invoice export drops rows", roster=r, cwd=d,
                                   activity="debug")
        skills = text.split("## Skills to load first", 1)[1]
        self.assertEqual(re.findall(r"^- `([^`]+)`", skills, re.M)[0], "root-cause")
        self.assertIn("Kind of work: Debugging and root-cause analysis (`debug`)", text)

    def test_the_members_who_fix_things_load_root_cause_in_their_first_phase(self):
        r = crew.load(team=False)
        for mid in FIXERS:
            m = crew.member(mid, r)
            with self.subTest(member=mid):
                self.assertEqual((m["procedure"][0].get("skills") or [None])[0], "root-cause")
                self.assertIn("root-cause", m["skills"])
        neo = {ph["phase"]: ph for ph in crew.member("neo", r)["procedure"]}
        for phase in ("Reproduce", "Root cause and design"):
            with self.subTest(phase=phase):
                self.assertIn("root-cause", neo[phase]["skills"])


class ResearchAndRequirementsTests(unittest.TestCase):
    def test_the_researcher_checks_what_is_known_first_and_records_what_it_found(self):
        r = crew.load(team=False)
        roland = crew.member("roland", r)
        self.assertEqual([ph["phase"] for ph in roland["procedure"]],
                         ["Question", "Prior knowledge", "Sources", "Spike", "Recommend", "Record", "Hand off"])
        phases = {ph["phase"]: ph for ph in roland["procedure"]}
        for phase in ("Prior knowledge", "Sources", "Record"):
            with self.subTest(phase=phase):
                self.assertIn("research", phases[phase]["skills"])
        self.assertIn("dev plan new", phases["Record"]["devkit"])
        self.assertIn("research", roland["skills"])
        self.assertIn("research", crew.member("architect", r)["skills"])

    def test_the_analyst_specifies_with_functional_analysis_and_hands_the_spec_on_by_role(self):
        oracle = {ph["phase"]: ph for ph in crew.member("oracle", crew.load(team=False))["procedure"]}
        self.assertIn("functional-analysis", oracle["Specify"]["skills"])
        for role in ("Software architect (The Architect)", "UX designer (Sati)", "QA engineer (Mouse)", "tech lead"):
            with self.subTest(role=role):
                self.assertIn(role, oracle["Hand off"]["do"])

    def test_the_skill_rules_require_each_new_skill_for_its_kinds_of_work(self):
        for activity, skill in (("debug", "root-cause"), ("research", "research"), ("architecture", "research"),
                                ("requirements", "functional-analysis")):
            for agent in ("claude", "codex"):
                with self.subTest(activity=activity, agent=agent):
                    need = [s for rule in skillguard.rules_for_activity(activity)
                            for s in skillguard.required(rule, agent)]
                    self.assertIn(skill, need)


if __name__ == "__main__":
    unittest.main()
