# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev crew graph`: the live session as one graph. The main session (Morpheus) at the root, the progress run and its
# steps as a DAG, the crew lanes under the steps they own (a nested subagent under the lane that launched it), the
# handoff work items the run links, and what waits: decisions, approvals owed, open messages. Drawn as a terminal
# tree, a Mermaid flowchart and JSON nodes and edges. Hermetic: progress state, the audit trail and the handoff work
# items live in temporary folders. Run: python3 -m unittest discover -s tests -p test_session_graph.py
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from devkit import crew, ui, work  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_crew import TempState, run_cli  # noqa: E402

T0 = int(time.time()) // 60 * 60 - 7200  # two hours ago: the sweep for lost subagents compares with the real clock
SAFE_ID = re.compile(r"^[a-z][a-z0-9]*$")
MERMAID_WORDS = {"end", "graph", "subgraph", "flowchart", "style", "class", "classDef", "click", "linkStyle",
                 "direction", "default"}


def m(minutes):
    return T0 + minutes * 60


def item(root, iid, title, bucket="now", status="todo", scope="acme/api"):
    it = {"meta": {"id": iid, "type": "task", "title": title, "scope": scope, "status": status, "bucket": bucket,
                   "priority": "normal", "links": [], "version": 1, "created": "2026-11-01T10:00:00Z"},
          "body": "", "changelog": []}
    p = root / scope / f"{iid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(work.render(it))


def launch(aid, kind, label, at):
    # Claude Code's sequence: the Agent call (PreToolUse), the agent's start, the call's result naming the agent
    pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}",
                                    "tool_input": {"description": label, "subagent_type": kind}}, now=at)
    pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind}, True, now=at)
    pg.launch_result("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}", "tool_response": {"agentId": aid}},
                     now=at)


# a finished member's report: its Check is the evidence its step ends done with (F6, R2-4)
DONE = ("**[Firefly> DevKit · Crew member]** api · `main` · step\n- **Done:** the step\n- **Check:** ✓ its tests pass\n"
        "- **Next:** Morpheus")


def stop(aid, kind, at):
    pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind, "last_assistant_message": DONE},
                   False, now=at)


def checkout_run(goal="Checkout flow (F-fxcheckout1)"):
    # 1 API contract (done, reopened and redone) -> 2 orders REST endpoint (Tank, done, so recheck) and
    # 3 checkout page component (Trinity, blocked on decision D1; Mouse runs inside Trinity's lane) -> 4 write the
    # docs (waiting for 3); 5 load test after 1 (skipped). A research lane owns no step. Seraph owes the approval of 2.
    pg.start(goal, plan="API contract=20m; orders REST endpoint=40m after API contract; "
                        "checkout page component=40m after API contract; "
                        "write the docs=15m after orders REST endpoint, checkout page component; "
                        "load test=30m after API contract", now=m(0))
    pg.step("API contract written", which="API contract", now=m(15))
    pg.assign("orders REST endpoint", "Tank", now=m(16))
    pg.assign("checkout page component", "Trinity", now=m(16))
    launch("a1", "devkit:tank", "Tank: orders endpoint", m(17))
    launch("a2", "devkit:trinity", "Trinity: checkout page", m(17))
    launch("a4", "general-purpose", "Codex settings research", m(18))
    stop("a4", "general-purpose", m(25))
    launch("a3", "devkit:mouse", "Mouse: probe payloads", m(30))
    stop("a1", "devkit:tank", m(50))
    pg.crew_decision("Card form: hosted or our own?", options="A: hosted | B: our own", recommend="A",
                     from_agent="a2", now=m(55))
    pg.crew_ask("Which currency format?", from_agent="a3", now=m(56))
    pg.reopen("load test", "no load in this release", skip=True, now=m(57))
    pg.reopen("API contract", "the contract missed refunds", now=m(58))
    pg.step("API contract covers refunds", which="API contract", now=m(65))
    run = pg.load_run("claude", "s1")
    run["agents"]["a3"]["parent"] = "a2"  # Mouse was launched by Trinity's subagent
    run["plan"][1]["links"] = ["B-fxrefunds01"]
    pg.run_path("claude", "s1").write_text(json.dumps(run))
    return run


class SessionGraphTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.state.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.items = Path(self.tmp.name) / "items"
        self.patch = mock.patch.object(work, "ITEMS", self.items)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.state.__exit__(None, None, None)

    def graph(self):
        return crew.session_graph(pg.load_run("claude", "s1"), now=m(70))

    def test_the_tree_shows_the_run_its_dag_the_lanes_under_their_steps_and_what_waits(self):
        item(self.items, "F-fxcheckout1", "Checkout flow", status="doing")
        item(self.items, "B-fxrefunds01", "Refunds missing from the contract")
        item(self.items, "T-fxunlinked1", "Unrelated now task")
        checkout_run()
        lines = [ln.rstrip() for ln in ui.ANSI_RE.sub("", crew.graph_text(self.graph())).splitlines()]
        expected = [
            "  ● Tech lead (Morpheus) · main session · claude s1",
            "  ├─ Run · Checkout flow (F-fxcheckout1)",
            "  │  ├─ ✓ 1. API contract · done · Tech lead (Morpheus) · [architecture]",
            "  │  ├─ ! 2. orders REST endpoint · done · recheck (reopened: API contract) · Backend engineer (Tank)"
            " · [backend] · after 1",
            "  │  │  └─ ○ Backend engineer (Tank) · orders REST endpoint · done · 33m",  # labeled by its step
            "  │  ├─ ✗ 3. checkout page component · blocked: waiting for decision D1 · Frontend engineer (Trinity)"
            " · [frontend] · after 1",
            "  │  │  └─ ● Frontend engineer (Trinity) · checkout page component · waiting for decision D1 · 53m",
            "  │  │     └─ ● QA engineer (Mouse) · probe payloads · working · 40m",  # as dev crew owner reads it
            "  │  ├─ ○ 4. write the docs · waiting for 3 · Tech lead (Morpheus) · [docs] · after 2, 3",
            "  │  ├─ – 1 skipped step: 5. load test · dev crew graph --all shows them",  # folded
            "  │  └─ Lanes with no step",
            "  │     └─ ○ general-purpose · Codex settings research · done · 7m",
            "  ├─ Work items linked to the run",
            "  │  ├─ F-fxcheckout1 · doing · Checkout flow · tracks the run",
            "  │  └─ B-fxrefunds01 · todo · Refunds missing from the contract · tracks step 2",
            "  ├─ Decisions waiting for the developer · dev crew decide <id> \"<answer>\"",
            "  │  └─ D1 · Frontend engineer (Trinity) asks: Card form: hosted or our own? · options A: hosted"
            " | B: our own · recommends A",
            "  ├─ Approvals owed",
            "  │  └─ Code reviewer (Seraph) approves 2. orders REST endpoint",
            "  └─ Open messages",
            "     └─ M1 · QA engineer (Mouse) → Tech lead (Morpheus) · not delivered: Which currency format?",
        ]
        missing = [ln for ln in expected if ln not in lines]
        self.assertEqual(missing, [], "\n".join(lines))
        self.assertEqual([lines.index(ln) for ln in expected], sorted(lines.index(ln) for ln in expected))
        status = lines[lines.index("  ├─ Run · Checkout flow (F-fxcheckout1)") + 1]
        self.assertTrue(status.startswith("  │  running · mode guided · 3/5 · "), status)
        self.assertIn("ETA ~", status)
        self.assertNotIn("Unrelated now task", "\n".join(lines))  # the run links its tasks: no Now list

    def test_the_mermaid_flowchart_has_every_dependency_as_an_edge_lanes_as_subgraphs_and_safe_ids(self):
        checkout_run()
        run = pg.load_run("claude", "s1")
        run["plan"][3]["name"] = 'write the "docs" <now>'  # a label that would break Mermaid unescaped
        pg.run_path("claude", "s1").write_text(json.dumps(run))
        rc, out, _ = run_cli("crew", "graph", "--latest", "--mermaid")  # one session's flowchart
        self.assertEqual(rc, 0)
        lines = [ln.strip() for ln in out.strip().splitlines()]
        self.assertEqual(lines[0], "flowchart TD")
        self.assertEqual(sum(ln.startswith("subgraph ") for ln in lines), sum(ln == "end" for ln in lines))
        defined = re.findall(r"^(?:subgraph )?([A-Za-z0-9_]+)(?:\[|\(|\{\{|>)", "\n".join(lines), re.M)
        for nid in defined:
            self.assertRegex(nid, SAFE_ID)
            self.assertNotIn(nid, MERMAID_WORDS)
            self.assertFalse(nid.startswith(("o", "x")), nid)  # "A---oB" would draw a circle edge
        self.assertEqual(len(defined), len(set(defined)), defined)
        for want in ("s1 --> s2", "s1 --> s3", "s2 --> s4", "s3 --> s4", "s1 --> s5"):
            self.assertIn(want, lines)
        self.assertEqual(sum(" --> " in ln and ln.startswith("s") for ln in lines), 5)
        self.assertIn('s4["○ 4. write the #quot;docs#quot; #lt;now#gt;<br/>waiting for 3 · Tech lead (Morpheus)'
                      ' · [docs]"]', lines)
        for ln in lines:
            for label in re.findall(r'"(.*?)"', ln):
                self.assertNotRegex(label.replace("<br/>", ""), r"[<>`]")  # the line break is the only markup

        def block(sub):  # the lines inside one subgraph, its nested subgraphs included
            i, depth, body = lines.index(next(ln for ln in lines if ln.startswith(f"subgraph {sub}["))), 0, []
            for ln in lines[i:]:
                depth += ln.startswith("subgraph ") - (ln == "end")
                body.append(ln)
                if depth == 0:
                    return body
        tank, trinity = block("lane1"), block("lane2")
        self.assertTrue(any(ln.startswith("s2[") for ln in tank), tank)
        self.assertTrue(any(ln.startswith("s3[") for ln in trinity), trinity)
        self.assertTrue(any(ln.startswith("subgraph lane4[") for ln in trinity), trinity)  # Mouse, nested
        self.assertFalse(any(ln.startswith(("s1[", "s4[", "s5[")) for ln in tank + trinity))
        self.assertIn("class s1 done", lines)
        self.assertIn("class s3 blocked", lines)

    def test_the_json_graph_has_the_nodes_and_edges_of_the_run(self):
        item(self.items, "F-fxcheckout1", "Checkout flow", status="doing")
        checkout_run()
        run = pg.load_run("claude", "s1")
        run["plan"][3]["after"] += ["checkout page component"]  # a run edited by hand repeats a dependency
        pg.run_path("claude", "s1").write_text(json.dumps(run))
        rc, out, _ = run_cli("crew", "graph", "--latest", "--json")  # one session's nodes and edges
        self.assertEqual(rc, 0)
        g = json.loads(out)
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(len(nodes), len(g["nodes"]))
        self.assertEqual(sorted(k for k, n in nodes.items() if n["kind"] == "step"), ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual({k: nodes[k]["state"] for k in ("s1", "s2", "s3", "s4", "s5")},
                         {"s1": "done", "s2": "done", "s3": "blocked", "s4": "waiting", "s5": "skipped"})
        self.assertEqual(nodes["s2"]["recheck"], ["API contract"])
        self.assertEqual((nodes["s2"]["owner"], nodes["s2"]["activity"]), ("Backend engineer (Tank)", "backend"))
        self.assertEqual((nodes["s4"]["after"], nodes["s4"]["waiting_for"]), (["s2", "s3"], ["s3"]))
        self.assertEqual(sum(1 for e in g["edges"] if e["kind"] == "after" and e["to"] == "s4"), 2)
        edges = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
        self.assertEqual({(f, t) for f, t, k in edges if k == "after"},
                         {("s1", "s2"), ("s1", "s3"), ("s2", "s4"), ("s3", "s4"), ("s1", "s5")})
        lanes = {n["ref"]: n for n in g["nodes"] if n["kind"] == "lane"}
        self.assertEqual({r: (n["who"], n["recorded"], n["parent"]) for r, n in lanes.items()},
                         {"a1": ("Backend engineer (Tank)", "done", None),
                          "a2": ("Frontend engineer (Trinity)", "running", None),
                          "a4": ("general-purpose", "done", None),
                          "a3": ("QA engineer (Mouse)", "running", lanes["a2"]["id"])})
        self.assertIn((lanes["a1"]["id"], "s2", "works_on"), edges)
        self.assertIn((lanes["a2"]["id"], "s3", "works_on"), edges)
        self.assertIn((lanes["a2"]["id"], lanes["a3"]["id"], "launched"), edges)
        self.assertIn(("main", lanes["a4"]["id"], "launched"), edges)
        self.assertEqual(lanes["a4"]["steps"], [])
        self.assertIn(("main", "run", "runs"), edges)
        self.assertEqual({t for f, t, k in edges if k == "step" and f == "run"}, {"s1", "s2", "s3", "s4", "s5"})
        by_kind = {}
        for n in g["nodes"]:
            by_kind.setdefault(n["kind"], []).append(n)
        self.assertEqual([(n["ref"], n["status"]) for n in by_kind["task"]], [("F-fxcheckout1", "doing")])
        task = by_kind["task"][0]["id"]
        self.assertIn((task, "run", "tracks"), edges)
        self.assertEqual(g["work_items"], "linked")
        dec, msg = by_kind["decision"][0], by_kind["message"][0]
        # F6: the developer also approves step 1, architecture work Morpheus did and approves (approvals.approver)
        appr = next(a for a in by_kind["approval"] if a["member"] == "seraph")
        self.assertEqual((dec["ref"], dec["recommend"]), ("D1", "A"))
        self.assertIn((lanes["a2"]["id"], dec["id"], "asks"), edges)
        self.assertEqual((appr["member"], appr["steps"]), ("seraph", ["s2"]))
        self.assertIn((appr["id"], "s2", "approves"), edges)
        self.assertEqual((msg["ref"], msg["state"]), ("M1", "not delivered"))
        self.assertIn((lanes["a3"]["id"], msg["id"], "sends"), edges)
        self.assertIn((msg["id"], "main", "to"), edges)
        self.assertEqual((g["run"]["goal"], g["run"]["mode"], g["run"]["state"]),
                         ("Checkout flow (F-fxcheckout1)", "guided", "running"))
        for e in g["edges"]:
            self.assertIn(e["from"], nodes)
            self.assertIn(e["to"], nodes)

    def test_a_run_that_links_no_work_item_lists_the_projects_now_tasks(self):
        item(self.items, "F-fxcheckout1", "Checkout flow", status="doing")
        item(self.items, "T-fxlater0001", "Someday task", bucket="later")
        item(self.items, "T-fxdone00001", "Finished task", status="done")
        checkout_run(goal="Checkout flow")
        run = pg.load_run("claude", "s1")
        run["plan"][1].pop("links")
        pg.run_path("claude", "s1").write_text(json.dumps(run))
        g = self.graph()
        tasks = [n for n in g["nodes"] if n["kind"] == "task"]
        self.assertEqual([t["ref"] for t in tasks], ["F-fxcheckout1"])
        self.assertEqual(g["work_items"], "now")
        self.assertFalse([e for e in g["edges"] if e["kind"] == "tracks"])
        text = ui.ANSI_RE.sub("", crew.graph_text(g))
        self.assertIn("├─ Now tasks of acme/api (the run links none)", text)
        self.assertIn("F-fxcheckout1 · doing · Checkout flow", text)

    def test_an_empty_session_says_there_is_no_run_and_how_to_start_one(self):
        with mock.patch.object(pg, "_project", lambda cwd=None: "acme/api"):
            rc, out, _ = run_cli("crew", "graph")
            self.assertEqual(json.loads(run_cli("crew", "graph", "--json")[1])["sessions"], [])
        self.assertEqual(rc, 0)
        self.assertIn("Crew graph · acme/api · 0 sessions", out)
        self.assertIn("No live progress run in this project", out)
        self.assertIn('dev progress start "<goal>" --plan', out)
        self.assertIn("No running progress run on this Mac", run_cli("crew", "graph", "--latest")[1])
        self.assertEqual(run_cli("crew", "graph", "--mermaid")[1].splitlines()[0], "flowchart TD")


if __name__ == "__main__":
    unittest.main()
