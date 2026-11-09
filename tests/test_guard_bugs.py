# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard's side of the bug registry: every refusal has a stable id that forms a bug fingerprint, a refusal for a
# known bug carries the workaround the registry serves to agents, the developer's own `dev bug` and `dev lesson
# release` decisions need their typed grant, and agents never post to the registry's issues around devkit's outbox.
# Run: python3 -m unittest tests.test_guard_bugs -v
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from devkit import audit, bugs, core, guard  # noqa: E402
import test_progress as tp  # noqa: E402  the real session, transcript and prompt hook (TranscriptCase)

GUARD_SRC = ROOT / "lib" / "devkit" / "guard.py"
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?::[A-Za-z0-9_-]{1,40})?")
REG = core.DEVKIT_REPO


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True,
                   capture_output=True)


def make_repo(root, name="repo", origin="") -> str:
    repo = Path(root) / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    return str(repo)


class DenyAll:
    def __call__(self, *a, **k):
        return None


def settings(main="enforce", member="enforce"):
    cfg = {"safeguards": {"irreversible_main": main, "irreversible_member": member}}
    return mock.patch.object(core, "load_config", return_value=cfg)


def deny_sites(tree):
    """[(refusal id, line)] for every deny(...) call in a module, named as deny() names it at run time: the
    innermost enclosing function (lambdas and comprehensions are not functions of their own), plus its key."""
    out = []

    def walk(node, fn):
        for ch in ast.iter_child_nodes(node):
            f = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(ch, ast.Call) and isinstance(ch.func, ast.Name) and ch.func.id == "deny":
                keys = [k for k in ch.keywords if k.arg == "key"]
                if len(ch.args) != 1 or (keys and not (isinstance(keys[0].value, ast.Constant)
                                                        and isinstance(keys[0].value.value, str))):
                    out.append((None, ch.lineno))  # a key that is no literal is no stable id
                else:
                    key = keys[0].value.value if keys else ""
                    out.append((f"{f}:{key}" if key else f, ch.lineno))
            walk(ch, f)
    walk(tree, "<module>")
    return out


class RefusalIds(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        root = Path(self.tmp.name)
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def check(self, cmd, tool="Bash", payload=None):
        with settings():
            return guard.check(tool, payload or {"command": cmd}, self.repo, "claude")

    def test_every_deny_site_has_a_stable_unique_id(self):
        sites = deny_sites(ast.parse(GUARD_SRC.read_text()))
        self.assertGreater(len(sites), 100)
        unstable = [line for rid, line in sites if rid is None or not ID_RE.fullmatch(rid)]
        self.assertEqual(unstable, [], "deny() sites whose id is not a function name plus a literal key")
        seen = {}
        dupes = []
        for rid, line in sites:
            if rid in seen:
                dupes.append(f"{rid} at lines {seen[rid]} and {line}")
            seen.setdefault(rid, line)
        self.assertEqual(dupes, [], "two deny() sites share one refusal id: give each its own key")

    def test_every_refusal_goes_through_deny(self):
        # a ("deny", …) tuple built by hand would carry no refusal id
        tree = ast.parse(GUARD_SRC.read_text())
        by_hand = []
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if fn.name == "deny":
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant) and \
                        node.elts[0].value == "deny" and isinstance(node.ctx, ast.Load) and len(node.elts) == 2:
                    by_hand.append(node.lineno)
        self.assertEqual(sorted(set(by_hand)), [])

    def test_a_refusal_is_named_by_its_site_and_its_command_word(self):
        v = self.check("cd . && git push --force origin feat/x")
        self.assertEqual(v[0], "deny")
        self.assertEqual(guard.LAST_REFUSAL, "check_git:force-push")
        self.assertEqual(guard.LAST_REFUSAL_WORD, "git")
        self.assertEqual(guard.refusal_of(v), ("check_git:force-push", "git"))

    def test_a_file_tool_refusal_has_an_id_and_no_command_word(self):
        v = self.check("", tool="Write", payload={"file_path": str(core.CONFIG_FILE), "content": "{}"})
        self.assertEqual(v[0], "deny")
        self.assertTrue(guard.LAST_REFUSAL and ID_RE.fullmatch(guard.LAST_REFUSAL), guard.LAST_REFUSAL)
        self.assertEqual(guard.LAST_REFUSAL_WORD, "")
        self.assertEqual(guard.refusal_of(v), (guard.LAST_REFUSAL, ""))

    def test_an_allowed_call_leaves_no_refusal(self):
        self.check("git push --force origin feat/x")
        self.assertIsNone(self.check("ls"))
        self.assertEqual((guard.LAST_REFUSAL, guard.LAST_REFUSAL_WORD), ("", ""))

    def test_a_denial_an_escape_hatch_skipped_is_no_refusal(self):
        # the rule computes its denial, then the hatch lets the call run: nothing was refused
        self.assertIsNone(self.check("DEVKIT_RAW_GH=1 gh pr merge 5"))
        self.assertEqual(guard.LAST_REFUSAL, "")

    def test_a_lambda_or_comprehension_takes_its_function_name(self):
        def probe_lambda():
            return (lambda: guard.deny("probe one"))()

        def probe_comprehension():
            return [guard.deny("probe two") for _ in (1,)][0]

        self.assertEqual(guard.refusal_of(probe_lambda())[0], "probe_lambda")
        self.assertEqual(guard.refusal_of(probe_comprehension())[0], "probe_comprehension")

    def test_a_verdict_the_guard_did_not_make_has_no_id(self):
        self.assertEqual(guard.refusal_of(("deny", "made elsewhere, never seen by deny()")), ("", ""))
        self.assertEqual(guard.refusal_of(None), ("", ""))


class RefusalNote(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "bugs"
        p = mock.patch.object(bugs, "BUGS", self.state)
        p.start()
        self.addCleanup(p.stop)
        self.reason = "Refusing to delete a .git directory (this destroys the repository history)."

    def cache(self, fp, detours, number=42, **rec):
        record = {"devkit_bug": 1, "fingerprint": fp, "kind": "guard", "title": "guard refusal check_rm:git-dir on rm",
                  "status": "open", "fixed_in": "", "detours": detours, **rec}
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "cache.json").write_text(json.dumps(
            {"ts": time.time(), "etag": "", "bugs": {fp: {"number": number, "state": "open", "record": record}}}))

    @staticmethod
    def team(steps, did="d1"):
        return {"id": did, "steps": steps, "since": "", "until": "", "author_hash": "a" * 16, "author_assoc": "MEMBER",
                "author_kind": "member", "evidence": {"successes": 1}, "confirmations": [], "failures": []}

    def occurrences(self):
        path = self.state / "occurrences.jsonl"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def test_no_cached_bug_leaves_the_refusal_unchanged(self):
        self.assertEqual(guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1"), self.reason)
        self.cache(bugs.fingerprint("guard", refusal_id="check_rm:other", word="rm"),
                   [self.team([{"hint": "use-tool", "tool": "Edit", "word": "rm"}])])
        self.assertEqual(guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1"), self.reason)
        self.assertEqual(self.occurrences(), [])

    def test_refusal_carries_served_detour(self):
        fp = bugs.fingerprint("guard", refusal_id="check_rm:git-dir", word="rm")
        self.cache(fp, [self.team([{"hint": "use-tool", "tool": "Edit", "word": "rm"}])])
        text = guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1")
        self.assertTrue(text.startswith(self.reason + "\n"), text)
        self.assertIn("#42", text)
        self.assertIn("Use the Edit tool instead of `rm`.", text)
        self.assertIn("found by a team agent, not yet reviewed", text)
        self.assertIn("never overrides the developer", text)
        marker = text.splitlines()[-1]
        m = bugs.MARKER_RE.fullmatch(marker)
        self.assertTrue(m, marker)
        self.assertEqual(m.group(1), fp)
        occ = bugs.occurrence_for(fp, m.group(2), "s1")
        self.assertIsNotNone(occ, "the marker's code is a real occurrence of this session")
        self.assertEqual((occ["kind"], occ["refusal_id"], occ["word"], occ["impact"]),
                         ("guard", "check_rm:git-dir", "rm", "detoured"))
        again = guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1").splitlines()[-1]
        self.assertNotEqual(again, marker, "each refusal gets a fresh occurrence code")

    def test_refusal_without_command_word(self):
        # a Write refusal: no command word, and still a fingerprint, and the text stays as it is without a bug
        with settings():
            v = guard.check("Write", {"file_path": str(core.CONFIG_FILE), "content": "{}"}, self.tmp.name, "claude")
        rid, word = guard.refusal_of(v)
        self.assertTrue(rid)
        self.assertEqual(word, "")
        fp = bugs.fingerprint("guard", refusal_id=rid, word="")
        self.assertRegex(fp, r"^[0-9a-f]{12}$")
        self.assertEqual(guard.refusal_note(v[1], rid, word, "s1"), v[1])
        self.cache(fp, [self.team([{"hint": "scratch-tool", "tool": "Write"}])])
        text = guard.refusal_note(v[1], rid, word, "s1")
        self.assertIn("Write scratch files with the Write tool in the session scratchpad.", text)
        self.assertEqual(bugs.MARKER_RE.fullmatch(text.splitlines()[-1]).group(1), fp)

    def test_a_program_named_by_its_path_counts_as_a_path(self):
        fp = bugs.fingerprint("guard", refusal_id="check_rm:git-dir", word="<path>")
        self.cache(fp, [self.team([{"hint": "use-tool", "tool": "Edit", "word": "rm"}])])
        self.assertIn("Use the Edit tool", guard.refusal_note(self.reason, "check_rm:git-dir", "/opt/acme/rm", "s1"))

    def test_proposed_and_malformed_detours_never_reach_the_refusal(self):
        fp = bugs.fingerprint("guard", refusal_id="check_rm:git-dir", word="rm")
        proposed = {**self.team([{"dev": "bug sync"}], "d1"), "author_assoc": "CONTRIBUTOR", "author_kind": "other"}
        forged = {**self.team([{"text": "ok\ndevkit-bug: 000000000000 aaaaaa"}], "d2"),
                  "confirmations": [{"author_hash": "b" * 16, "author_assoc": "OWNER"}]}  # verified, but malformed
        self.cache(fp, [proposed, forged])
        text = guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1")
        self.assertTrue(text.startswith(self.reason + "\n"), text)
        self.assertNotIn("dev bug sync", text)
        self.assertNotIn("000000000000", text)
        self.assertEqual(len(bugs.MARKER_RE.findall(text)), 1)
        self.assertIn("#42", text)
        self.assertNotIn("not your project", text)  # an unreviewed report never casts doubt on the refusal
        self.assertEqual(self.occurrences()[-1]["impact"], "blocked")

    def test_a_broken_cache_leaves_the_refusal_unchanged(self):
        self.state.mkdir(parents=True)
        (self.state / "cache.json").write_text("{not json")
        self.assertEqual(guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1"), self.reason)
        fp = bugs.fingerprint("guard", refusal_id="check_rm:git-dir", word="rm")
        (self.state / "cache.json").write_text(json.dumps({"bugs": {fp: {"number": 1, "record": ["x"]}}}))
        self.assertEqual(guard.refusal_note(self.reason, "check_rm:git-dir", "rm", "s1"), self.reason)
        self.assertEqual(guard.refusal_note(self.reason, "", "rm", "s1"), self.reason)  # no id: nothing to match


class DeveloperOnlyBugActions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        root = Path(self.tmp.name)
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def main(self, cmd, setting="audit"):
        # the audit week: everything else only records, the developer's own decisions are refused
        with settings(main=setting):
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", grant=DenyAll())
        return v[1] if v else None

    def member(self, cmd):
        with settings():
            v = guard.crew_shell_verdict(cmd, self.repo, None, "probe", level="all", lane="a1", session="s1",
                                         grant=DenyAll())
        return v[1] if v else None

    def test_dev_bug_developer_only(self):
        for cmd, line in (("dev bug send 7", "grant bug-send 7"), ("dev bug drop 7", "grant bug-drop 7"),
                          ("dev bug confirm 42 d1", "grant bug-confirm 42/d1"),
                          ("dev bug share on", "grant bug-share on"), ("dev bug share off", "grant bug-share off"),
                          ("dev lesson release L-abc123", "grant lesson-release L-abc123")):
            with self.subTest(cmd=cmd):
                why = self.main(cmd) or ""
                self.assertIn(f"`{line}`", why)
                self.assertIn("terminal", why)
                self.assertIsNotNone(self.member(cmd))
        for cmd in ("dev bug", "dev bug ls --open", "dev bug show 42", "dev bug top", "dev bug sync",
                    "dev bug pending", "dev bug search sed", "dev bug share", "dev bug send --help",
                    "dev lesson review", "dev lesson ls"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.main(cmd, setting="enforce"))

    def test_a_bug_action_without_its_target_cannot_be_granted(self):
        why = self.main("dev bug send") or ""
        self.assertIn("developer", why)
        self.assertNotIn("grant bug-send", why)

    def test_unparsed_text_naming_a_bug_action_is_refused(self):
        cmd = 'dev bug send 7 "unterminated'
        self.assertIsNone(guard.irreversible_actions(cmd, self.repo))
        self.assertEqual(self.main(cmd), guard.UNPARSED_REASON)

    def test_config_set_bugs_share_refused(self):
        for cmd in ("dev config set bugs.share on", "dev config set bugs.share=off", "dev config set BUGS.share ''",
                    "dev config set bugs '{\"share\": \"on\"}'"):
            with self.subTest(cmd=cmd):
                with settings():
                    v = guard.check("Bash", {"command": cmd}, self.repo, "claude")
                self.assertEqual((v or ("",))[0], "deny", cmd)
                self.assertIn("dev bug share", v[1])
                self.assertEqual(guard.LAST_REFUSAL, "dev:config-bugs-share")
        with settings():
            self.assertIsNone(guard.check("Bash", {"command": "dev config set progress.max_parallel 6"}, self.repo,
                                          "claude"))


class DeveloperGrantLines(tp.TranscriptCase):
    # the developer opens one of these decisions by typing its grant line: the real prompt hook, transcript and
    # grant store, single use, bound to its exact target

    def setUp(self):
        super().setUp()
        root = Path(self.state.tmp.name)
        self.repo = make_repo(root)
        for p in (mock.patch.object(guard, "current_project", return_value="acme/api"),  # a grant holds in one project
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json"),
                  mock.patch.object(core, "STATE", root / "state")):
            p.start()
            self.addCleanup(p.stop)
        self.typed = 0

    def type_line(self, text):
        self.typed += 1
        pid = f"prompt-bug-{self.typed}"
        self.write({"type": "user", "promptId": pid, "uuid": f"u-bug-{self.typed}", "isSidechain": False,
                    "origin": {"kind": "human"}, "message": {"role": "user", "content": text}})
        return self.prompt(text, pid)

    def main(self, cmd):
        with settings():
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1")
        return v[1] if v else None

    def test_each_typed_grant_opens_exactly_its_target_once(self):
        for line, cmd, other in (("grant bug-send 7", "dev bug send 7", "dev bug send 8"),
                                 ("grant bug-drop 7", "dev bug drop 7", "dev bug drop 8"),
                                 ("grant bug-confirm 42/d1", "dev bug confirm 42 d1", "dev bug confirm 42 d2"),
                                 ("grant bug-share on", "dev bug share on", "dev bug share reports"),
                                 ("grant lesson-release L-abc", "dev lesson release L-abc", "dev lesson release L-x")):
            with self.subTest(line=line):
                self.type_line(line)
                self.assertIsNotNone(self.main(other))
                self.assertIsNone(self.main(cmd))
                self.assertIn(line, self.main(cmd) or "")  # used once

    def test_no_typed_grant_opens_a_registry_write(self):
        for line in ("grant pr-write issue/new", "grant registry-write issue/new", "grant developer-only issue/new"):
            self.type_line(line)
        self.assertIn("dev bug report", self.main(f"gh issue create -R {REG} --title t --body b") or "")


class RegistryWrites(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = make_repo(root, "other", origin="https://github.com/acme/widget.git")
        self.devkit = make_repo(root, "devkit", origin=f"git@github.com:{REG}.git")
        (root / "body.json").write_text("{}")
        (root / "handoff" / "projects").mkdir(parents=True)  # no embargo anywhere: temp folders, no project
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
                  mock.patch.object(core, "HANDOFF", root / "handoff"),
                  mock.patch.object(core, "NOTES", root / "handoff" / "projects"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def main(self, cmd, cwd=None, setting="audit"):
        with settings(main=setting):
            v = guard.irreversible_verdict(cmd, cwd or self.repo, agent="claude", session="s1", grant=DenyAll())
        return v[1] if v else None

    def member(self, cmd, cwd=None):
        with settings():
            v = guard.crew_shell_verdict(cmd, cwd or self.repo, None, "probe", level="all", lane="a1", session="s1",
                                         grant=DenyAll())
        return v[1] if v else None

    def test_agent_gh_writes_to_registry_refused(self):
        upper = REG.upper()
        for cmd in (f"gh issue create -R {REG} --title t --body b",
                    f"gh issue comment 5 --repo {REG} --body x",
                    f"gh issue comment 5 --repo={upper} --body x",
                    f"gh -R {REG} issue close 5",
                    f"GH_REPO={REG} gh issue reopen 5",
                    f"gh issue edit https://github.com/{REG}/issues/5 --title x",
                    f"gh issue delete 5 -R github.com/{REG} --yes",
                    f"gh issue lock 5 -R {REG}",
                    f"gh api repos/{REG}/issues/5/comments -f body=x",
                    f"gh api /repos/{REG}/issues -X POST --input body.json",
                    f"gh api -X PATCH repos/{upper}/issues/comments/9 -f body=y",
                    f"gh api --method DELETE repos/{REG}/issues/comments/9",
                    f"gh api https://api.github.com/repos/{REG}/issues/5/comments -f body=x",
                    f"gh api repos/{REG}/pulls/../issues/5/comments -f body=x",
                    f"gh api repos/{REG}/%69ssues/5/comments -f body=x",
                    f"bash -c 'gh issue comment 5 -R {REG} -b x'",
                    f"dev gh issue comment 5 -R {REG} -b x"):
            with self.subTest(cmd=cmd):
                self.assertIn("devkit's bug registry", self.main(cmd) or "")
                self.assertIn("dev bug report", self.member(cmd) or "")
        # reads, other repositories and pull requests keep the usual rules
        for cmd in (f"gh issue view 5 -R {REG}", f"gh issue list -R {REG}",
                    f"gh api repos/{REG}/issues/5/comments", f"gh api -X GET repos/{REG}/issues -f state=open",
                    f"gh pr create -R {REG} --title t --body b"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.main(cmd, setting="enforce"))
        for cmd in ("gh issue comment 5 -R acme/widget -b x", f"gh pr comment 5 -R {REG} -b x",
                    "gh api repos/acme/widget/issues/5/comments -f body=x",
                    f"gh api repos/{REG}devkit2/issues -f title=t"):
            with self.subTest(cmd=cmd):
                why = self.main(cmd, setting="enforce") or ""
                self.assertIn("grant ", why)  # the usual tier-2 gate, which the developer may grant
                self.assertNotIn("bug registry", why)

    def test_a_checkout_of_devkit_posts_to_the_registry_without_naming_it(self):
        for cmd in ("gh issue create --title t --body b", "gh issue comment 5 -b x",
                    "gh api repos/{owner}/{repo}/issues -f title=t"):
            with self.subTest(cmd=cmd):
                self.assertIn("devkit's bug registry", self.main(cmd, cwd=self.devkit) or "")
                self.assertIsNone(self.main(cmd))  # in another repository: the audit week only records it

    def test_http_clients_cannot_post_to_the_registry(self):
        url = f"https://api.github.com/repos/{REG}/issues/5/comments"
        for cmd in (f"curl -X POST -H 'Authorization: token x' {url} -d '{{}}'",
                    f"curl --data @body.json {url}",
                    f"wget --post-data=x {url}",
                    f"http POST {url} body=x",
                    f"curl -X PATCH https://API.GITHUB.COM/repos/{REG}/issues/comments/9 -d x"):
            with self.subTest(cmd=cmd):
                self.assertIn("devkit's bug registry", self.main(cmd) or "")
        self.assertIsNone(self.main(f"curl {url}", setting="enforce"))  # a read
        why = self.main("curl -X POST https://api.github.com/repos/acme/widget/issues -d x", setting="enforce") or ""
        self.assertIn("grant ", why)
        self.assertNotIn("bug registry", why)

    def test_a_gate_refusal_has_its_id_and_command_word(self):
        with settings(main="enforce"):
            reg = guard.irreversible_verdict(f"gh issue comment 5 -R {REG} -b x", self.repo, agent="claude",
                                             session="s1", grant=DenyAll())
            other = guard.irreversible_verdict("gh issue comment 5 -R acme/widget -b x", self.repo, agent="claude",
                                               session="s1", grant=DenyAll())
        self.assertEqual(guard.refusal_of(reg), ("_main_gate:registry", "gh"))
        self.assertEqual(guard.refusal_of(other), ("_main_gate:no-grant", "gh"))

    def test_a_github_tool_cannot_post_to_the_registry(self):
        owner, name = REG.split("/")
        for tool, inp in (("mcp__github__add_issue_comment", {"owner": owner, "repo": name, "issue_number": 5}),
                          ("mcp__github__create_issue", {"owner": owner.upper(), "repo": name, "title": "t"}),
                          ("mcp__github__update_issue", {"repo": REG, "issue_number": 5})):
            with self.subTest(tool=tool):
                with settings(main="audit"):
                    v = guard.irreversible_tool_verdict(tool, inp, self.repo, agent="claude", session="s1",
                                                        grant=DenyAll())
                self.assertIn("devkit's bug registry", (v or ("", ""))[1])
        with settings(main="audit"):
            self.assertIsNone(guard.irreversible_tool_verdict(
                "mcp__github__add_issue_comment", {"owner": "acme", "repo": "widget", "issue_number": 5}, self.repo,
                agent="claude", session="s1", grant=DenyAll()))


class CommandWords(unittest.TestCase):

    def test_command_words_are_the_programs_the_guard_reasons_about(self):
        words = guard.COMMAND_WORDS
        self.assertIsInstance(words, frozenset)
        for w in ("git", "gh", "dev", "sed", "rm", "find", "xargs", "python3", "bash", "tee", "pytest", "claude"):
            self.assertIn(w, words)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z][A-Za-z0-9._+-]{0,30}", w) for w in words))
        for w in ("acme-internal-tool", "/usr/bin/sed", ""):
            self.assertNotIn(w, words)


if __name__ == "__main__":
    unittest.main()
