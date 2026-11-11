# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Which skill rules a changed file triggers (config/skill-rules.json through skillguard.rules_for_files), as a fixture
# table: each path with its content and the rule ids it must trigger. A glob that misses its intended files, or a
# content pattern that fires on files of another kind, fails here. Tests carry comments, not docstrings: `dev test
# prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_skillguard
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, gates, skillguard as sg  # noqa: E402

# A repository that does not use Postgres: a Laravel app on MySQL, devkit-like docs and skills, and code of each stack.
APP = {
    "composer.json": '{"require": {"laravel/framework": "^11.0"}}',
    ".env.example": "DB_CONNECTION=mysql\n",
    "docs/crew.md": "# The crew\n",
    "docs/guides/setup.md": "# Setup\n",
    "README.md": "# App\n",
    "CHANGELOG.md": "# Changelog\n",
    "VERSION": "26.11.7\n",
    "plugin/skills/testing/SKILL.md": "---\nname: testing\n---\n",
    "plugin/skills/delivery/references/flow.md": "# Flow\n",
    "plugin/agents/neo.md": "---\nname: neo\n---\n",
    "plugin/commands/ship.md": "# Ship\n",
    "agents/AGENTS.md": "# Global instructions\n",
    "tests/test_page.py": 'HTML = "<!doctype html><style>p{}</style>"\n',
    "lib/report.py": 'PAGE = "<!doctype html><style>body{}</style>"\n',
    "lib/shout.py": 'PAGE = "<!DOCTYPE html><HTML><STYLE>P{}</STYLE>"\n',
    "lib/tool.py": "def main():\n    return 0\n",
    "lib/junit.py": 'SAMPLE = \'<testcase classname="tests.test_calc" name="test_add" time="0.001"/>\'\n',
    "lib/term.py": "def lines(rows):\n    styled = []\n    for r in rows:\n        styled.append(r.upper())\n    return styled\n",
    "web/src/Button.js": 'export const Button = () => <button className="primary">Go</button>;\n',
    "web/src/Title.js": "export const Title = styled.h1`\n  color: red;\n`;\n",
    "src/main/java/acme/AuditSink.java": 'import org.springframework.stereotype.Component;\n@Component("auditSink")\n'
                                         "class AuditSink {}\n",
    "web/src/app/hero.component.ts": "@Component({\n  selector: 'app-hero',\n})\nexport class Hero {}\n",
    "app/Http/Controllers/UserController.php": "<?php\nclass UserController {}\n",
    "routes/api.php": "<?php\nRoute::get('/users', [UserController::class, 'index']);\n",
    "service/main.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/items')\ndef items():\n    return []\n",
    "server/routes.ts": "const router = express.Router();\nrouter.post('/orders', create);\n",
    "database/migrations/2024_01_01_create_users_table.php": "<?php\nSchema::create('users', fn ($t) => $t->id());\n",
    "infra/main.tf": 'resource "aws_s3_bucket" "b" {}\n',
    ".github/workflows/ci.yml": "on: push\n",
}
EXPECT_APP = {
    "docs/crew.md": ["docs"],
    "docs/guides/setup.md": ["docs"],
    "README.md": ["docs"],
    "CHANGELOG.md": ["docs"],
    "VERSION": ["release"],
    "plugin/skills/testing/SKILL.md": ["docs"],
    "plugin/skills/delivery/references/flow.md": ["docs"],
    "plugin/agents/neo.md": ["docs"],
    "plugin/commands/ship.md": ["docs"],
    "agents/AGENTS.md": ["docs"],
    "tests/test_page.py": ["testing"],
    "lib/report.py": ["ui"],
    "lib/shout.py": ["ui"],
    "lib/tool.py": [],
    "lib/junit.py": [],
    "lib/term.py": [],
    "web/src/Button.js": ["ui"],
    "web/src/Title.js": ["ui"],
    "src/main/java/acme/AuditSink.java": ["spring"],
    "web/src/app/hero.component.ts": ["angular", "ui"],
    "app/Http/Controllers/UserController.php": ["api"],
    "routes/api.php": ["api"],
    "service/main.py": ["api"],
    "server/routes.ts": ["api"],
    "database/migrations/2024_01_01_create_users_table.php": ["database"],
    "infra/main.tf": ["security", "terraform"],
    ".github/workflows/ci.yml": ["workflows"],
}
# A repository that uses Postgres: its migrations need the Postgres skill too.
PG = {
    "docker-compose.yml": "services:\n  db:\n    image: postgres:16\n",
    "src/main/resources/db/migration/V1__init.sql": "create table orders (id bigint primary key);\n",
}
EXPECT_PG = {"src/main/resources/db/migration/V1__init.sql": ["database", "postgres"]}


def make_repo(root: Path, files: dict) -> Path:
    (root / ".git").mkdir(parents=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


class Groups:
    # the skill groups this Mac installs (skills.groups), for the rules that need one
    def __init__(self, *groups):
        self.groups = list(groups)

    def __enter__(self):
        self.saved = json.loads(json.dumps(core.CFG.get("skills") or {}))
        core.CFG.setdefault("skills", {})["groups"] = self.groups
        return self

    def __exit__(self, *exc):
        core.CFG["skills"] = self.saved


def tool_use(name, **inp):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


class RuleFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = make_repo(Path(self.tmp.name) / "app", APP)
        self.pg = make_repo(Path(self.tmp.name) / "pg", PG)

    def tearDown(self):
        self.tmp.cleanup()

    def ids_by_file(self, root: Path, rels) -> dict:
        out = {rel: [] for rel in rels}
        for rule, files in sg.rules_for_files([str(root / rel) for rel in rels]):
            for f in files:
                out[str(Path(f).relative_to(root))].append(rule["id"])
        return {rel: sorted(ids) for rel, ids in out.items()}

    def assert_rules(self, keys, root=None, expect=None):
        root, expect = root or self.app, expect or EXPECT_APP
        got = self.ids_by_file(root, keys)
        wrong = {k: f"got {got[k]}, want {sorted(expect[k])}" for k in keys if got[k] != sorted(expect[k])}
        self.assertEqual(wrong, {})

    # every doc and agent instruction file needs the docs skill: top-level docs, skills, agents, commands, AGENTS.md
    def test_docs_rule_covers_docs_skills_agents_and_instructions(self):
        self.assert_rules(["docs/crew.md", "docs/guides/setup.md", "README.md", "plugin/skills/testing/SKILL.md",
                           "plugin/skills/delivery/references/flow.md", "plugin/agents/neo.md",
                           "plugin/commands/ship.md", "agents/AGENTS.md"])

    # a changelog entry is documentation; a version bump or a release config is release work
    def test_changelog_is_docs_and_version_is_release(self):
        self.assert_rules(["CHANGELOG.md", "VERSION"])

    # the UI rule fires on code that renders a page, never on a test that holds HTML or on a Spring @Component("x")
    def test_ui_rule_fires_on_ui_code_only(self):
        self.assert_rules(["tests/test_page.py", "lib/report.py", "lib/tool.py", "src/main/java/acme/AuditSink.java",
                           "web/src/app/hero.component.ts"])

    # JSX's className and styled-components' styled.<tag>` are case-sensitive code: a Python module that holds JUnit
    # XML (classname=) or builds a list named styled is no UI, while HTML in any case and real components still are
    def test_ui_rule_reads_component_tokens_as_written(self):
        self.assert_rules(["lib/junit.py", "lib/term.py", "lib/shout.py", "lib/report.py", "web/src/Button.js",
                           "web/src/Title.js"])

    # service code in PHP, Python and Node needs the backend skill, like a Java controller does; a plain module not
    def test_service_code_needs_the_backend_skill(self):
        self.assert_rules(["app/Http/Controllers/UserController.php", "routes/api.php", "service/main.py",
                           "server/routes.ts", "lib/tool.py"])

    # a migration needs the backend skill everywhere, and the Postgres skill only where the project uses Postgres
    def test_postgres_skill_only_where_the_project_uses_postgres(self):
        mysql = "database/migrations/2024_01_01_create_users_table.php"
        pg = "src/main/resources/db/migration/V1__init.sql"
        self.assert_rules([mysql])
        self.assert_rules([pg], root=self.pg, expect=EXPECT_PG)
        need = {rel: sorted({s for r, _ in sg.rules_for_files([str(root / rel)]) for s in sg.required(r, "claude")})
                for root, rel in ((self.app, mysql), (self.pg, pg))}
        self.assertEqual(need, {mysql: ["backend"], pg: ["backend", "supabase-postgres-best-practices"]})

    # a rule of an optional skill group (Terraform's, group iac) applies only when that group is installed
    def test_a_group_rule_applies_only_with_its_group(self):
        with Groups("security-deep", "postgres", "observability", "iac"):
            self.assert_rules(["infra/main.tf"])
        with Groups("security-deep", "postgres", "observability"):
            self.assertEqual(self.ids_by_file(self.app, ["infra/main.tf"]), {"infra/main.tf": ["security"]})


class OutsideTheRepository(unittest.TestCase):
    def setUp(self):
        self._installed = sg.installed
        sg.installed = lambda agent: {"frontend-design", "impeccable", "testing", "docs"}

    def tearDown(self):
        sg.installed = self._installed

    # a scratch file outside every repository is not delivered work: no rule asks for a skill at Stop
    def test_stop_does_not_judge_a_scratch_file(self):
        with tempfile.TemporaryDirectory() as d:
            scratch = Path(d) / "scratchpad" / "probe.py"
            scratch.parent.mkdir()
            scratch.write_text('HTML = "<!doctype html><style>p{}</style>"\n')
            repo = make_repo(Path(d) / "repo", {"lib/page.py": 'PAGE = "<style>p{}</style>"\n'})
            t = Path(d) / "t.jsonl"
            t.write_text(tool_use("Write", file_path=str(scratch), content="x") + "\n")
            self.assertIsNone(sg.on_stop("claude", {"transcript_path": str(t)}, {}))
            t.write_text(tool_use("Write", file_path=str(repo / "lib" / "page.py"), content="x") + "\n")
            why = sg.on_stop("claude", {"transcript_path": str(t)}, {})
            self.assertIn("`frontend-design`", why or "")
            self.assertIn("page.py", why or "")

    # a probe in a scratch folder is scratch work even inside a repository built there (a fixture repo for a test,
    # say): the UI rule reads no Python in the session scratchpad or devkit's .tmp, and still reads it in a real repo
    def test_a_python_probe_in_a_scratch_folder_is_not_ui_work(self):
        html = 'PAGE = "<!doctype html><style>p{}</style>"\n'
        with tempfile.TemporaryDirectory() as d:
            probes = [make_repo(Path(d) / "scratchpad" / "kid" / "fixture", {"probe.py": html}) / "probe.py",
                      make_repo(Path(d) / "Development" / ".tmp" / "fixture", {"lib/probe.py": html}) / "lib" / "probe.py"]
            real = make_repo(Path(d) / "Development" / "acme" / "app", {"lib/page.py": html}) / "lib" / "page.py"
            ui = [f for r, m in sg.rules_for_files([str(p) for p in probes + [real]]) if r["id"] == "ui" for f in m]
            self.assertEqual(ui, [str(real)])


class CommandsAtStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._installed = sg.installed
        sg.installed = lambda agent: {"delivery", "release-policy", "testing", "learning-loop", "cicd",
                                      "github-actions-hardening"}

    def tearDown(self):
        sg.installed = self._installed
        self.tmp.cleanup()

    def stop(self, *lines, agent="claude", sess=None):
        t = Path(self.tmp.name) / "t.jsonl"
        t.write_text("\n".join(lines) + "\n")
        return sg.on_stop(agent, {"transcript_path": str(t)}, {} if sess is None else sess)

    # a turn that pushed and ran the tests owes delivery and testing, named with the command that needs each
    def test_a_turn_that_ran_commands_owes_their_skills(self):
        why = self.stop(tool_use("Bash", command="git push -u origin fix/x"),
                        tool_use("Bash", command="cd /repo && bin/dev test"))
        self.assertIn("`delivery`", why or "")
        self.assertIn("`testing`", why or "")
        self.assertIn("git push", why or "")
        self.assertNotIn("release-policy", why or "")  # a push is not a release

    # a tag is a release; Codex's shell calls count the same way
    def test_a_codex_turn_that_tagged_owes_the_release_skills(self):
        call = json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "git tag v26.11.7"})}})
        why = self.stop(call, agent="codex")
        self.assertIn("`release-policy`", why or "")
        self.assertIn("`delivery`", why or "")

    # Codex 0.167 runs shell commands inside its code-mode `exec` script (a real rollout's record): they count the same
    def test_a_codex_code_mode_push_owes_the_delivery_skill(self):
        call = json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "status": "completed", "call_id": "call_fb4583ade17e43f49e878334175021a6",
            "name": "exec", "input": "text(await tools.exec_command({cmd:\"git push origin main\","
                                     "max_output_tokens:1000}));\n"}})
        why = self.stop(call, agent="codex")
        self.assertIn("`delivery`", why or "")
        self.assertIn("git push", why or "")

    # a push whose text the exec script builds at run time shows only in its CommandExecution item (0.167.0 capture)
    def test_a_codex_push_built_at_run_time_owes_the_delivery_skill(self):
        script = json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "status": "completed", "call_id": "call_1", "name": "exec",
            "input": "const b = \"main\";\ntext(await tools.exec_command({cmd: `git push origin ${b}`}));\n"}})
        item = json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1", "item": {
            "type": "CommandExecution", "id": "exec-5f125236-dd03-45f9-8fd3-db673fc2b9b0",
            "command": ["/bin/zsh", "-lc", "git push origin main"], "cwd": "file:///repo", "status": "completed",
            "exit_code": 0}}})
        why = self.stop(script, item, agent="codex")
        self.assertIn("`delivery`", why or "")
        self.assertIn("git push", why or "")

    # text that only mentions a command, reading a CI run and listing lessons owe nothing; each skill is asked once
    def test_mentions_and_reads_owe_nothing_and_each_skill_is_asked_once(self):
        self.assertIsNone(self.stop(tool_use("Bash", command='echo "git push"'),
                                    tool_use("Bash", command="gh run view 42 --log-failed"),
                                    tool_use("Bash", command="dev lesson ls")))
        sess = {}
        self.assertIn("`learning-loop`", self.stop(tool_use("Bash", command='dev lesson add "x" --cause y'),
                                                   sess=sess) or "")
        self.assertIsNone(self.stop(tool_use("Bash", command='dev lesson add "z" --cause w'), sess=sess))



# A spawned Codex agent's rollout, shaped like the real scout's (Codex 0.167.0, 2026-11-09,
# rollout-2026-11-09T10-00-55-01a1c178-64c7-7ac1-81c6-ec8b852e1772.jsonl): its session_meta names the parent
# (forked_from_id) and where its own history starts (subagent_history_start_ordinal); then a copy of the parent's
# history: the parent's session_meta, its turn with the exec call that read a skill and ran a command, the spawn and
# the turn_aborted that ends the copy; then the agent's own records, from its task_started.
PARENT, SCOUT = "01a1c178-2665-7ca1-9a5d-046a95cee8bc", "01a1c178-64c7-7ac1-81c6-ec8b852e1772"
PARENT_TURN, SCOUT_TURN = "01a1c178-28b7-7fa1-a67c-2bd8b7433d66", "01a1c178-651e-7bc1-a532-a2415b26e099"
PARENT_SCRIPT = ("text(await tools.exec_command({cmd:\"cat /Users/me/.codex/plugins/cache/openai-curated-remote/superpowers/"
                 "7.0.1/skills/using-superpowers/SKILL.md\",\"max_output_tokens\":3000}));\n"
                 "text(await tools.exec_command({cmd:\"python3 -c 'import nosuchmod'\",\"max_output_tokens\":1000}));\n")
SCOUT_SCRIPT = ("const r = await tools.exec_command({cmd:\"rg --files\",\"workdir\":\"/repo\",\"max_output_tokens\":1200});"
                "\ntext(r.output);\n")


def _scout_rollout(path):
    recs = [
        ("session_meta", {"id": SCOUT, "forked_from_id": PARENT, "parent_thread_id": PARENT, "cli_version": "0.167.0",
                          "thread_source": "subagent", "agent_role": "scout", "agent_path": "/root/list_files",
                          "subagent_history_start_ordinal": 9}),
        ("session_meta", {"id": PARENT, "cli_version": "0.167.0", "source": "exec", "thread_source": "user"}),
        ("event_msg", {"type": "task_started", "turn_id": PARENT_TURN,
                       "turn_attribution": {"turn_id": PARENT_TURN, "turn_trigger": "exec", "parent_turn_id": None}}),
        ("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Run python3 -c 'import nosuchmod', then spawn a scout agent to list the files"}]}),
        ("response_item", {"type": "custom_tool_call", "status": "completed", "call_id": "call_eac2505c", "name": "exec",
                           "input": PARENT_SCRIPT}),
        ("response_item", {"type": "custom_tool_call_output", "call_id": "call_eac2505c",
                           "output": [{"type": "input_text", "text": "Script completed\n"}]}),
        ("response_item", {"type": "function_call", "status": "completed", "name": "spawn_agent",
                           "namespace": "collaboration", "arguments": json.dumps(
                               {"task_name": "list_files", "agent_type": "scout", "message": "gAAAAABq8gpX"})}),
        ("event_msg", {"type": "turn_aborted", "turn_id": PARENT_TURN, "reason": "interrupted"}),
        ("response_item", {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "You are now a newly spawned child agent. The preceding conversation is "
                                           "inherited parent context."}]}),
        ("event_msg", {"type": "thread_settings_applied", "thread_id": SCOUT}),
        ("event_msg", {"type": "task_started", "turn_id": SCOUT_TURN, "turn_attribution": {
            "turn_id": SCOUT_TURN, "turn_trigger": "exec", "parent_turn_id": PARENT_TURN,
            "initiating_agent_path": "/root"}}),
        ("response_item", {"type": "custom_tool_call", "status": "completed", "call_id": "call_c2cc5bca", "name": "exec",
                           "input": SCOUT_SCRIPT}),
        ("event_msg", {"type": "item_completed", "thread_id": SCOUT, "turn_id": SCOUT_TURN, "item": {
            "type": "CommandExecution", "id": "exec-723551dd-b4f5-454b-aa9e-dd740a2de705",
            "command": ["/bin/zsh", "-lc", "rg --files"], "cwd": "file:///repo", "status": "completed", "exit_code": 0}}),
    ]
    Path(path).write_text("".join(json.dumps({"timestamp": "2026-11-09T18:00:55.302Z", "ordinal": i, "type": t,
                                              "payload": p}) + "\n" for i, (t, p) in enumerate(recs)))


class ForkedCodexRollout(unittest.TestCase):
    # a member is credited with its own commands and skills only, never with the parent's history its rollout copies
    def test_a_spawned_agent_is_credited_only_with_its_own_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "rollout.jsonl")
            _scout_rollout(path)
            loaded, _, commands, _ = sg.read("codex", path)
            self.assertEqual(commands, ["rg --files"])
            self.assertNotIn("using-superpowers", loaded)
            self.assertEqual(gates._activity(path, "codex"), (["rg --files"], []))  # what the crew gates count

if __name__ == "__main__":
    unittest.main()
