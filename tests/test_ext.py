# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev ext` (spec P-fym583l7sg1): the catalog of third-party plugins, skills and MCP servers, what is installed, the
# retirement of Context7, and the team configuration's own consistency. Hermetic: fixture configurations, temporary
# agent homes, an injected command runner; no network and no real `claude` or `codex`.
# Run: python3 -m unittest tests.test_ext -v
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, cli, core, ext, hooks, setup, ui  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402

SHA_A = "414603bf14c7a344edb466bb1f85690c77ce8b7d"
SHA_B = "bb92a77741419a4ab5f06e711a283343f1ada0c3"


def fixture_team() -> dict:
    claude = {
        "marketplaces": {"claude-plugins-official": "anthropics/claude-plugins-official", "firefly-devkit": "/x"},
        "plugins": ["devkit@firefly-devkit", "superpowers@claude-plugins-official",
                    "security-guidance@claude-plugins-official"],
        "disabled": ["php-lsp@claude-plugins-official"],
        "retired": ["context7@claude-plugins-official"],
        "mcp": {"chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@1.12.0", "--no-x"]}},
        "meta": {
            "devkit@firefly-devkit": {"why": "devkit itself", "license": "Apache-2.0", "required": True},
            "superpowers@claude-plugins-official": {"why": "process skills", "license": "MIT",
                                                    "upstream": "obra/superpowers"},
            "security-guidance@claude-plugins-official": {"why": "warnings", "license": "Apache-2.0",
                                                          "required": True},
            "php-lsp@claude-plugins-official": {"why": "PHP", "license": "Apache-2.0"},
            "mcp:chrome-devtools": {"why": "browser debugging", "license": "Apache-2.0",
                                    "upstream": "npm:chrome-devtools-mcp"},
        },
    }
    codex = {
        "plugins": ["superpowers@openai-curated-remote"],
        "retired": ["mcp:context7"],
        "mcp": {"docs": {"url": "https://docs.example.com/mcp"}},
        "meta": {"superpowers@openai-curated-remote": {"why": "process skills", "license": "MIT"},
                 "mcp:docs": {"why": "docs", "license": "MIT"}},
    }
    skills = {"sources": [
        {"repo": "pbakaus/impeccable", "ref": SHA_A, "license": "Apache-2.0", "agents": ["claude"],
         "why": "UI quality", "skills": {"impeccable": ".claude/skills/impeccable"}},
        {"repo": "mattpocock/skills", "ref": SHA_B, "license": "MIT", "_why": "design vocabulary",
         "skills": {"codebase-design": "skills/engineering/codebase-design"}},
        {"repo": "supabase/agent-skills", "ref": SHA_B, "license": "MIT", "why": "Postgres", "group": "postgres",
         "skills": {"supabase-postgres-best-practices": "skills/x"}},
    ]}
    return {"claude": claude, "codex": codex, "skills": skills}


def by_id(cat) -> dict:
    return {e.id: e for e in cat}


class FakeRun:
    """The command runner devkit's ext module is given: records each argv and answers from a table."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        return self.answers.get(tuple(argv), (0, "", ""))


def make_homes(root: Path) -> "ext.Homes":
    h = ext.Homes(claude=root / ".claude", codex=root / ".codex", agents_skills=root / ".agents" / "skills",
                  skills_share=root / "share" / "skills", claude_json=root / ".claude.json")
    for d in (h.claude / "plugins", h.codex, h.agents_skills, h.skills_share):
        d.mkdir(parents=True, exist_ok=True)
    return h


def install_claude_plugin(h, pid: str, version: str, sha: str, enabled=True):
    f = h.claude / "plugins" / "installed_plugins.json"
    doc = json.loads(f.read_text()) if f.exists() else {"version": 2, "plugins": {}}
    doc["plugins"][pid] = [{"scope": "user", "version": version, "gitCommitSha": sha,
                            "installPath": str(h.claude / "plugins" / "cache" / pid)}]
    f.write_text(json.dumps(doc))
    s = h.claude / "settings.json"
    settings = json.loads(s.read_text()) if s.exists() else {}
    settings.setdefault("enabledPlugins", {})[pid] = enabled
    s.write_text(json.dumps(settings))


def plain(s: str) -> str:
    return ui.ANSI_RE.sub("", s)


class CatalogTests(unittest.TestCase):
    def test_each_kind_gets_its_id_source_and_pin_kind(self):
        cat = by_id(ext.catalog(fixture_team(), {}, {"postgres"}))
        sp = cat["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((sp.kind, sp.agent, sp.name, sp.source, sp.pin_kind, sp.license, sp.why),
                         ("plugin", "claude", "superpowers", "claude-plugins-official", "observed", "MIT",
                          "process skills"))
        self.assertEqual(cat["plugin:claude:devkit@firefly-devkit"].pin_kind, "local")
        im = cat["skill:claude:impeccable"]
        self.assertEqual((im.source, im.pin, im.pin_kind, im.upstream),
                         ("pbakaus/impeccable", SHA_A, "sha", "pbakaus/impeccable"))
        cd = cat["mcp:claude:chrome-devtools"]
        self.assertEqual((cd.source, cd.pin, cd.pin_kind, cd.upstream),
                         ("npm chrome-devtools-mcp", "1.12.0", "version", "npm:chrome-devtools-mcp"))
        docs = cat["mcp:codex:docs"]
        self.assertEqual((docs.pin_kind, docs.source), ("url", "docs.example.com"))

    def test_a_skill_without_agents_is_one_entry_per_agent_and_reads_the_old_why_key(self):
        cat = by_id(ext.catalog(fixture_team(), {}, set()))
        self.assertIn("skill:claude:codebase-design", cat)
        self.assertIn("skill:codex:codebase-design", cat)
        self.assertNotIn("skill:codex:impeccable", cat)  # agents: ["claude"]
        self.assertEqual(cat["skill:codex:codebase-design"].why, "design vocabulary")

    def test_a_group_skill_is_in_the_catalog_only_when_its_group_is_chosen(self):
        self.assertNotIn("skill:claude:supabase-postgres-best-practices",
                         by_id(ext.catalog(fixture_team(), {}, set())))
        self.assertIn("skill:claude:supabase-postgres-best-practices",
                      by_id(ext.catalog(fixture_team(), {}, {"all"})))

    def test_team_disabled_plugins_are_in_the_catalog_but_off(self):
        cat = by_id(ext.catalog(fixture_team(), {}, set()))
        self.assertFalse(cat["plugin:claude:php-lsp@claude-plugins-official"].enabled)
        self.assertTrue(cat["plugin:claude:superpowers@claude-plugins-official"].enabled)

    def test_retired_entries_leave_the_catalog_unless_the_user_adds_them_back(self):
        team = fixture_team()
        team["claude"]["plugins"].append("context7@claude-plugins-official")  # still listed by mistake
        self.assertNotIn("plugin:claude:context7@claude-plugins-official", by_id(ext.catalog(team, {}, set())))
        user = {"add": [{"kind": "plugin", "agent": "claude", "id": "context7@claude-plugins-official",
                         "license": "MIT", "why": "I use it"}]}
        kept = by_id(ext.catalog(team, user, set()))["plugin:claude:context7@claude-plugins-official"]
        self.assertEqual((kept.layer, kept.why), ("user", "I use it"))

    def test_a_team_mcp_server_without_an_exact_version_is_refused(self):
        team = fixture_team()
        team["claude"]["mcp"]["loose"] = {"command": "npx", "args": ["-y", "loose-mcp@latest"]}
        with self.assertRaises(ext.ExtError) as cm:
            ext.catalog(team, {}, set())
        self.assertIn("mcp:claude:loose", str(cm.exception))


class TeamConfigTests(unittest.TestCase):
    """The shipped configuration: every entry says why it is here and under which license (spec R1)."""

    def setUp(self):
        self.cat = ext.catalog(ext.team(), {}, {"all"})

    def test_every_team_entry_has_a_why_and_a_license(self):
        missing = [e.id for e in self.cat if not e.why.strip() or not e.license.strip()]
        self.assertEqual(missing, [])

    def test_the_required_security_plugins_and_devkit_are_marked_required(self):
        required = {e.id for e in self.cat if e.required}
        self.assertTrue({"plugin:claude:devkit@firefly-devkit",
                         "plugin:claude:security-guidance@claude-plugins-official",
                         "plugin:claude:claude-security@claude-plugins-official",
                         "plugin:codex:codex-security@openai-curated-remote"} <= required, required)

    def test_context7_is_gone_from_the_team_configuration_and_the_global_rules(self):
        for name in ("claude/plugins.json", "codex/plugins.json", "skills.json"):
            doc = json.loads((ROOT / "config" / name).read_text())
            doc.pop("retired", None)
            self.assertNotIn("context7", json.dumps(doc).lower(), name)
        self.assertNotIn("context7", (ROOT / "agents" / "AGENTS.md").read_text().lower())
        self.assertIn("context7@claude-plugins-official",
                      json.loads((ROOT / "config" / "claude" / "plugins.json").read_text())["retired"])
        self.assertIn("mcp:context7", json.loads((ROOT / "config" / "codex" / "plugins.json").read_text())["retired"])

    def test_metadata_names_only_entries_the_team_lists(self):
        for agent in ("claude", "codex"):
            conf = ext.team()[agent]
            listed = set(conf.get("plugins", [])) | set(conf.get("disabled", [])) | \
                {f"mcp:{n}" for n in conf.get("mcp", {})}
            self.assertEqual(sorted(set(conf.get("meta", {})) - listed), [], agent)


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = make_homes(Path(self.tmp.name))

    def test_reads_claude_plugins_with_version_sha_and_enabled_state(self):
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.0", SHA_B, enabled=False)
        o = ext.observe(self.h)["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((o["installed"], o["version"], o["sha"], o["enabled"]), (True, "7.0.0", SHA_B, False))

    def test_reads_codex_plugins_from_the_cache_and_mcp_servers_from_config_toml(self):
        for v in ("7.0.0", "7.0.10", "7.0.9"):
            (self.h.codex / "plugins" / "cache" / "openai-curated-remote" / "superpowers" / v).mkdir(parents=True)
        (self.h.codex / "config.toml").write_text(
            '[plugins."superpowers@openai-curated-remote"]\nenabled = false\n\n'
            '[mcp_servers.context7]\nurl = "https://mcp.context7.com/mcp"\n\n'
            '[mcp_servers.chrome-devtools]\ncommand = "npx"\nargs = ["-y", "chrome-devtools-mcp@1.12.0"]\n')
        o = ext.observe(self.h)
        sp = o["plugin:codex:superpowers@openai-curated-remote"]
        self.assertEqual((sp["installed"], sp["version"], sp["enabled"]), (True, "7.0.10", False))
        self.assertTrue(o["mcp:codex:context7"]["installed"])
        self.assertEqual(o["mcp:codex:chrome-devtools"]["version"], "1.12.0")

    def test_reads_claude_mcp_servers_and_the_skill_source_markers(self):
        self.h.claude_json.write_text(json.dumps({"mcpServers": {"chrome-devtools": {
            "command": "npx", "args": ["-y", "chrome-devtools-mcp@1.11.0"]}}}))
        sk = self.h.skills_share / "impeccable"
        sk.mkdir()
        (sk / ".devkit-source").write_text(f"pbakaus/impeccable@{SHA_A} .claude/skills/impeccable license=x\n")
        (sk / "SKILL.md").write_text("# impeccable\n")
        (self.h.claude / "skills").mkdir()
        (self.h.claude / "skills" / "impeccable").symlink_to(sk)
        o = ext.observe(self.h)
        self.assertEqual(o["mcp:claude:chrome-devtools"]["version"], "1.11.0")
        self.assertEqual((o["skill:claude:impeccable"]["installed"], o["skill:claude:impeccable"]["ref"]),
                         (True, SHA_A))
        self.assertFalse(o.get("skill:codex:impeccable", {}).get("installed", False))


class StatusTests(unittest.TestCase):
    def test_a_vendor_plugin_shows_the_version_and_sha_installed(self):
        cat = ext.catalog(fixture_team(), {}, set())
        observed = {"plugin:claude:superpowers@claude-plugins-official":
                    {"installed": True, "version": "7.0.0", "sha": SHA_B, "enabled": True}}
        row = {r["id"]: r for r in ext.status(cat, observed)}["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((row["pin_kind"], row["state"], row["version"], row["sha"]),
                         ("observed", "installed", "7.0.0", SHA_B))
        self.assertEqual(ext.pin_text(row), "⚠ observed 7.0.0 bb92a77")

    def test_a_missing_entry_and_a_retired_one_still_installed_are_drift(self):
        team = fixture_team()
        cat = ext.catalog(team, {}, set())
        observed = {"plugin:claude:context7@claude-plugins-official": {"installed": True, "version": "1", "sha": ""}}
        rows = {r["id"]: r for r in ext.status(cat, observed, retired=ext.retired_ids(team))}
        self.assertEqual(rows["mcp:claude:chrome-devtools"]["state"], "missing")
        self.assertEqual([d["kind"] for d in rows["mcp:claude:chrome-devtools"]["drift"]], ["missing"])
        self.assertEqual(rows["plugin:claude:context7@claude-plugins-official"]["layer"], "retired")
        self.assertEqual([d["kind"] for d in rows["plugin:claude:context7@claude-plugins-official"]["drift"]],
                         ["retired"])


class RetireTests(unittest.TestCase):
    """Context7 from 26.11.3: `dev install` uninstalls it from both agents and audits each removal (spec R5)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = make_homes(Path(self.tmp.name))
        install_claude_plugin(self.h, "context7@claude-plugins-official", "0412be76f50a", SHA_B)
        (self.h.codex / "config.toml").write_text('[mcp_servers.context7]\nurl = "https://mcp.context7.com/mcp"\n')

    def test_the_plan_uninstalls_the_claude_plugin_and_removes_the_codex_mcp_server(self):
        team = fixture_team()
        acts = ext.plan(ext.catalog(team, {}, set()), ext.observe(self.h), ext.retired_ids(team))
        self.assertEqual(sorted(acts), [
            ("retire", "mcp:codex:context7", ["codex", "mcp", "remove", "context7"]),
            ("retire", "plugin:claude:context7@claude-plugins-official",
             ["claude", "plugin", "uninstall", "context7@claude-plugins-official", "-s", "user"])])

    def test_nothing_to_retire_when_it_is_not_installed_or_the_user_keeps_it(self):
        team = fixture_team()
        empty = make_homes(Path(self.tmp.name) / "empty")
        self.assertEqual(ext.plan(ext.catalog(team, {}, set()), ext.observe(empty), ext.retired_ids(team)), [])
        user = {"add": [{"kind": "plugin", "agent": "claude", "id": "context7@claude-plugins-official",
                         "license": "MIT", "why": "mine"}]}
        acts = ext.plan(ext.catalog(team, user, set()), ext.observe(self.h), ext.retired_ids(team))
        self.assertEqual([a[1] for a in acts], ["mcp:codex:context7"])

    def test_apply_runs_each_command_and_writes_one_ext_retire_record_per_entry(self):
        team = fixture_team()
        acts = ext.plan(ext.catalog(team, {}, set()), ext.observe(self.h), ext.retired_ids(team))
        run = FakeRun()
        before = len(audit.records(days=1, action="ext.retire"))
        msgs = ext.apply(acts, run)
        self.assertEqual(sorted(c[0] for c in run.calls), ["claude", "codex"])
        recs = audit.records(days=1, action="ext.retire")[before:]
        self.assertEqual(sorted(r["target"] for r in recs),
                         ["mcp:codex:context7", "plugin:claude:context7@claude-plugins-official"])
        self.assertEqual([ok for ok, _ in msgs], [True, True])

    def test_a_failed_removal_is_reported_and_audited_as_failed(self):
        team = fixture_team()
        acts = [a for a in ext.plan(ext.catalog(team, {}, set()), ext.observe(self.h), ext.retired_ids(team))
                if a[1].startswith("mcp:")]
        run = FakeRun({("codex", "mcp", "remove", "context7"): (1, "", "no such server")})
        (ok, msg), = ext.apply(acts, run)
        self.assertFalse(ok)
        self.assertIn("no such server", msg)
        self.assertNotEqual(audit.records(days=1, action="ext.retire")[-1]["outcome"], "ok")

    def test_retire_for_one_agent_touches_only_that_agent(self):
        run = FakeRun()
        ext.retire("codex", run, self.h, fixture_team(), {})
        self.assertEqual(run.calls, [["codex", "mcp", "remove", "context7"]])

    def test_an_invalid_overlay_still_drops_retired_keys(self):
        bad = {"add": [{"kind": "mcp", "name": "x", "args": ["-y", "x-mcp"], "license": "MIT", "why": "w"}]}
        self.assertEqual(ext.retired_plugin_keys("claude", fixture_team(), bad), {"context7@claude-plugins-official"})

    def test_claude_settings_drop_the_enabled_key_of_a_retired_plugin(self):
        f = core.CLAUDE_HOME / "settings.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"enabledPlugins": {"context7@claude-plugins-official": True, "mine@x": True}}))
        setup.write_claude_settings()
        ep = json.loads(f.read_text())["enabledPlugins"]
        self.assertNotIn("context7@claude-plugins-official", ep)
        self.assertTrue(ep["mine@x"])
        self.assertTrue(ep["superpowers@claude-plugins-official"])


class LockAndDriftTests(unittest.TestCase):
    """superpowers changed outside devkit (lock 7.0.0, installed 7.0.1): the session start, `dev status` and
    `dev doctor` name it, and one ext.drift record is written (spec R6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.h = make_homes(root)
        self.lock, self.seen = root / "ext-lock.json", root / "ext-drift.json"
        self.team = fixture_team()
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.0", SHA_B)

    def rows(self):
        return ext.status(ext.catalog(self.team, {}, set()), ext.observe(self.h), ext.read_lock(self.lock))

    def drift_records(self):
        return [r for r in audit.records(days=1, action="ext.drift") if r["target"].endswith("superpowers@"
                                                                                             "claude-plugins-official")]

    def test_dev_install_records_what_the_vendor_plugins_are_now(self):
        n = ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        lk = ext.read_lock(self.lock)["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((n, lk["pin"], lk["version"], lk["sha"], lk["by"]), (1, "observed", "7.0.0", SHA_B,
                                                                              "dev install"))
        self.assertNotIn("plugin:claude:devkit@firefly-devkit", ext.read_lock(self.lock))  # local: nothing to lock

    def test_a_change_outside_devkit_is_drift_with_both_versions(self):
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.1", SHA_A)
        row = {r["id"]: r for r in self.rows()}["plugin:claude:superpowers@claude-plugins-official"]
        (d,) = row["drift"]
        self.assertEqual((d["kind"], d["from"], d["to"]), ("changed", "7.0.0 bb92a77", "7.0.1 414603b"))
        self.assertEqual(ext.pin_text(row), "⚠ observed 7.0.0 bb92a77")  # the lock, until the developer accepts

    def test_no_drift_before_the_first_lock_or_when_nothing_changed(self):
        self.assertEqual([d for r in self.rows() for d in r["drift"] if d["kind"] == "changed"], [])
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        self.assertEqual([d for r in self.rows() for d in r["drift"] if d["kind"] == "changed"], [])

    def test_each_drift_writes_one_audit_record_however_often_it_is_seen(self):
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.1", SHA_A)
        before = len(self.drift_records())
        first = ext.report_drift(self.rows(), seen=self.seen)
        ext.report_drift(self.rows(), seen=self.seen)
        self.assertEqual(len(first), 1)
        recs = self.drift_records()[before:]
        self.assertEqual(len(recs), 1)
        self.assertIn("7.0.0 bb92a77 → 7.0.1 414603b", recs[0]["detail"])

    def test_a_drift_that_ended_and_came_back_is_recorded_again(self):
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.1", SHA_A)
        ext.report_drift(self.rows(), seen=self.seen)
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)  # accepted
        self.assertEqual(ext.report_drift(self.rows(), seen=self.seen), [])
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.2", SHA_B)
        self.assertEqual(len(ext.report_drift(self.rows(), seen=self.seen)), 1)

    def test_the_drift_text_names_the_plugin_and_the_change(self):
        ext.record_lock("claude", self.h, t=self.team, user={}, path=self.lock)
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.1", SHA_A)
        self.assertEqual(ext.drift_text(self.rows()),
                         "superpowers (claude plugin) 7.0.0 bb92a77 → 7.0.1 414603b outside devkit")

    def test_a_required_plugin_turned_off_in_a_project_is_drift(self):
        rows = ext.status(ext.catalog(self.team, {}, set()), {}, {},
                          project={"security-guidance@claude-plugins-official": False, "stranger@x": True})
        by = {r["id"]: r for r in rows}
        self.assertTrue(by["plugin:claude:security-guidance@claude-plugins-official"]["enabled"])
        self.assertEqual([d["kind"] for d in by["plugin:claude:stranger@x"]["drift"]], ["not-in-catalog"])

    def test_a_required_plugin_turned_off_in_the_agent_itself_is_drift(self):
        observed = {"plugin:claude:security-guidance@claude-plugins-official":
                    {"installed": True, "version": "2.0.18", "sha": SHA_B, "enabled": False}}
        row = {r["id"]: r for r in ext.status(ext.catalog(self.team, {}, set()), observed)}[
            "plugin:claude:security-guidance@claude-plugins-official"]
        self.assertIn("required-off", [d["kind"] for d in row["drift"]])

    def test_the_lock_file_and_the_config_ext_key_are_protected_from_agents(self):
        from devkit import guard
        for path, content in ((core.STATE / "ext-lock.json", "{}"),
                              (core.CONFIG_FILE, json.dumps({"ext": {"remove": ["x"]}}))):
            with self.subTest(path=str(path)):
                v = guard.check("Write", {"file_path": str(path), "content": content}, str(core.DEV), "claude")
                self.assertEqual(v[0] if v else "allow", "deny")


class DriftWiringTests(unittest.TestCase):
    """The session start and `dev status` read the same rows; the session start reaches no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = make_homes(Path(self.tmp.name))
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.0", SHA_B)
        lk = core.STATE / "ext-lock.json"
        lk.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: lk.unlink(missing_ok=True))
        self.addCleanup(lambda: (core.STATE / "ext-drift.json").unlink(missing_ok=True))
        ext.record_lock("claude", self.h)
        install_claude_plugin(self.h, "superpowers@claude-plugins-official", "7.0.1", SHA_A)

    def test_the_session_line_names_the_change_and_how_to_accept_it(self):
        line = ext.session_line(str(self.tmp.name), self.h)
        self.assertIn("superpowers (claude plugin) 7.0.0 bb92a77 → 7.0.1 414603b", line)
        self.assertIn("dev install", line)

    def test_dev_status_needs_you_names_the_change(self):
        with mock.patch.object(ext, "homes", return_value=self.h):
            needs = cli.devkit_needs(str(self.tmp.name))
        self.assertEqual([n["command"] for n in needs if "superpowers" in n["text"]], ["dev ext ls"])

    def test_dev_install_records_the_lock_after_installing(self):
        with mock.patch.object(ext, "homes", return_value=self.h), mock.patch.object(setup, "sh",
                                                                                      lambda *a, **k: (0, "", "")):
            setup._extensions("claude")
        lk = ext.read_lock()["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((lk["version"], lk["sha"]), ("7.0.1", SHA_A))

    def test_doctor_fails_the_lock_check_and_names_the_plugin(self):
        with mock.patch.object(ext, "homes", return_value=self.h):
            rows = {name: (ok, detail) for _, name, ok, detail, _ in ext.doctor_checks(str(self.tmp.name))}
        ok, detail = rows["vendor plugins match the lock"]
        self.assertFalse(ok)
        self.assertIn("superpowers", detail)


class DriftSessionStartTests(HookCase):
    def setUp(self):
        super().setUp()
        DriftWiringTests.setUp(self)

    def test_the_session_start_hook_adds_the_line_and_writes_one_drift_record(self):
        before = len([r for r in audit.records(days=1, action="ext.drift") if "superpowers" in r["target"]])
        with mock.patch.object(ext, "homes", return_value=self.h), \
                mock.patch.object(hooks, "context_for", lambda cwd, agent: "## devkit · workspace context"), \
                mock.patch.object(hooks, "_spawn", lambda *a, **k: None), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None):
            outs = [self.hook("session-start", {"session_id": s, "hook_event_name": "SessionStart",
                                                "source": "startup", "cwd": str(self.tmp.name)}) for s in ("a", "b")]
        self.assertIn("superpowers (claude plugin) 7.0.0 bb92a77 → 7.0.1 414603b", outs[0])
        self.assertIn("superpowers", outs[1])  # every session sees it until the developer accepts it
        after = [r for r in audit.records(days=1, action="ext.drift") if "superpowers" in r["target"]]
        self.assertEqual(len(after) - before, 1)


class LsCommandTests(unittest.TestCase):
    def ls(self, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["ext", "ls", *args]), 0)
        return out.getvalue()

    def test_ls_json_gives_every_field_of_each_entry(self):
        rows = json.loads(self.ls("--json"))
        self.assertTrue(rows)
        need = {"id", "source", "pin", "pin_kind", "state", "enabled", "license", "layer", "why", "version", "sha"}
        self.assertEqual([r["id"] for r in rows if not need <= set(r)], [])
        sp = next(r for r in rows if r["id"] == "plugin:claude:superpowers@claude-plugins-official")
        self.assertEqual((sp["pin_kind"], sp["layer"]), ("observed", "team"))

    def test_ls_agent_filter_shows_one_agent(self):
        self.assertEqual({r["agent"] for r in json.loads(self.ls("--agent", "codex", "--json"))}, {"codex"})

    def test_ls_table_has_the_columns_and_marks_unpinned_entries(self):
        lines = plain(self.ls("--agent", "claude")).splitlines()
        self.assertEqual(lines[0].split()[:4], ["NAME", "KIND", "SOURCE", "PIN"])
        line = next(ln for ln in lines if ln.startswith("superpowers "))
        self.assertIn("⚠ observed", line)


if __name__ == "__main__":
    unittest.main()
