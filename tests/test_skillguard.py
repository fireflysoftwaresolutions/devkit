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

from devkit import core, skillguard as sg  # noqa: E402

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
    "lib/tool.py": "def main():\n    return 0\n",
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
    "VERSION": ["delivery"],
    "plugin/skills/testing/SKILL.md": ["docs"],
    "plugin/skills/delivery/references/flow.md": ["docs"],
    "plugin/agents/neo.md": ["docs"],
    "plugin/commands/ship.md": ["docs"],
    "agents/AGENTS.md": ["docs"],
    "tests/test_page.py": ["testing"],
    "lib/report.py": ["ui"],
    "lib/tool.py": [],
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


if __name__ == "__main__":
    unittest.main()
