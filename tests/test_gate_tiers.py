# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# F5 (spec v17, C6; security consultation §3): the irreversible-action gate. The guard sorts what a command runs, at
# every level the shell runs it (bash -c, sh -c, eval, a pipe or a here-string into a shell, the substitutions it
# expands, an interpreter that starts a process), into action classes in two tiers. Tier 1 is a push of a branch that
# is not the default branch, without force, and a PR create; tier 2 is everything that cannot be taken back: a
# destructive push, PR and issue writes, merge, release, publish, deploy, API writes and external messages. Crew
# members never run tier 2, granted or not, and run tier 1 only with a step grant; the main session runs tier 1 and
# needs a live grant for tier 2 once `safeguards.irreversible_main` enforces. Grants come from Tank's C5 store
# (progress.use_grant): these tests stand in a store of their own at that boundary, and its own rules (expiry,
# evidence, single use in the store) are tested on Tank's side. Text a command only carries is not an action.
# Run: python3 -m unittest tests.test_gate_tiers -v
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

DAY = 86400.0


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True,
                   capture_output=True)


def make_repo(root) -> str:
    # a repository on feat/x whose origin's default branch is main, with a release tag, an npm package and a Python
    # project: what the classifier reads to name a target
    repo = Path(root) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "package.json").write_text(json.dumps({"name": "@acme/widget", "version": "1.4.0"}))
    (repo / "pyproject.toml").write_text("[project]\nname = \"acme-widget\"\nversion = \"2.0.1\"\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "tag", "v26.11.4")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    return str(repo)


class Store:
    # stands in for progress.use_grant: grants by "action target", each with its uses left
    def __init__(self, grants=None):
        self.left = dict(grants or {})
        self.calls = []

    def __call__(self, action, target, repo, consume):
        self.calls.append((action, target, consume))
        key = f"{action} {target}"
        if self.left.get(key, 0) <= 0:
            return None
        if consume:
            self.left[key] -= 1
        return {"id": "G1", "source": "E2", "uses_left": self.left[key], "expires": time.time() + 600}


class Broken:
    def __call__(self, *a, **k):
        raise RuntimeError("state busy")


def settings(main=None, member=None):
    cfg = {"safeguards": {k: v for k, v in (("irreversible_main", main), ("irreversible_member", member)) if v}}
    return mock.patch.object(core, "load_config", return_value=cfg)


class GateCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        self.records = []
        p = mock.patch.object(guard, "_record", lambda *a, **k: self.records.append((a, k)))
        p.start()
        self.addCleanup(p.stop)

    def acts(self, cmd, cwd=None):
        found = guard.irreversible_actions(cmd, cwd or self.repo)
        return None if found is None else [(a["action"], a["tier"], a["target"]) for a in found]

    def member(self, cmd, lane="a1", grant=None, who="tank", setting=None):
        with settings(member=setting):
            v = guard.crew_shell_verdict(cmd, self.repo, None, who, lane=lane, session="s1", grant=grant or Store())
        return v[1] if v else None

    def main(self, cmd, grant=None, setting="enforce"):
        with settings(main=setting):
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", grant=grant or Store())
        return v[1] if v else None

    def gate_records(self):
        # (target, outcome, the record's fields) of each guard.irreversible record
        return [(a[1], a[3], k) for a, k in self.records if a[0] == "guard.irreversible"]


class ClassifyTests(GateCase):

    def test_a_feature_branch_push_without_force_and_a_pr_create_are_tier_1(self):
        for cmd in ("git push", "git push origin feat/x", "git push -u origin HEAD", "dev push", "bin/dev push",
                    "git -C . push origin feat/x:feat/x", "dev git push origin feat/x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("push", 1, "feat/x")])
        for cmd in ("gh pr create --fill", "dev pr create --title 'feat: x'", "gh pr create -H feat/x -t x -b y"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("pr", 1, "feat/x")])

    def test_a_push_to_the_default_branch_a_force_and_a_delete_are_destructive(self):
        for cmd, target in (("git push origin main", "main"), ("git push origin HEAD:main", "main"),
                            ("git push origin feat/x:refs/heads/master", "master"),
                            ("git push --force-with-lease", "feat/x"), ("git push origin +feat/x", "feat/x"),
                            ("dev push --force-with-lease", "feat/x"), ("git push origin --delete feat/old", "feat/old"),
                            ("git push origin :feat/old", "feat/old"), ("git push -d origin feat/old", "feat/old"),
                            ("git push --mirror", "mirror"), ("git push --all origin", "all-branches")):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("push-destructive", 2, target)])
        _git(self.repo, "checkout", "-q", "main")
        self.assertEqual(self.acts("git push"), [("push-destructive", 2, "main")])

    def test_a_push_to_a_repository_on_this_disk_is_not_an_action(self):
        bare = Path(self.tmp.name) / "mirror.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        _git(self.repo, "remote", "add", "mirror", str(bare))
        for cmd in ("git push mirror main", f"git push {bare} feat/x", f"git push file://{bare} --delete feat/x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])
        _git(self.repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
        self.assertEqual(self.acts("git push origin main"), [("push-destructive", 2, "main")])

    def test_tag_pushes_and_releases_are_a_release(self):
        for cmd, target in (("git push origin v26.11.4", "v26.11.4"), ("git push origin refs/tags/v1.0", "v1.0"),
                            ("git push --tags", "all-tags"), ("dev release --version 26.11.5", "v26.11.5"),
                            ("gh release create v1.2.0 --notes x", "v1.2.0"), ("gh release upload v1.2.0 a.zip", "v1.2.0"),
                            ("gh release delete v1.2.0 -y", "v1.2.0")):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("release", 2, target)])

    def test_merges_and_pr_and_issue_writes_name_their_pr_or_issue(self):
        for cmd, act in (("gh pr merge 12 --squash", ("merge", 2, "12")), ("dev pr merge 12", ("merge", 2, "12")),
                         ("dev pr merge", ("merge", 2, "feat/x")),
                         ("gh pr merge https://github.com/o/r/pull/12", ("merge", 2, "12")),
                         ("gh pr comment 12 -b x", ("pr-write", 2, "pr/12")),
                         ("gh pr close 12", ("pr-write", 2, "pr/12")), ("dev pr review 3 --approve", ("pr-write", 2, "pr/3")),
                         ("gh issue create -t x -b y", ("pr-write", 2, "issue/new")),
                         ("gh issue comment 7 --body x", ("pr-write", 2, "issue/7")),
                         ("dev issue create --title x", ("pr-write", 2, "issue/new"))):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [act])
        for cmd in ("gh pr view 12", "gh pr checks 12", "gh pr list", "gh issue list", "dev pr checks --watch",
                    "gh pr checkout 12", "gh release view v1", "gh release download v1"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_gh_api_with_field_is_a_write(self):
        for cmd in ("gh api repos/o/r/issues -f title=x", "gh api repos/o/r/issues -F title=x",
                    "gh api repos/o/r/issues --field title=x", "gh api repos/o/r/issues --raw-field title=x",
                    "gh api repos/o/r/issues --input body.json", "gh api -X DELETE repos/o/r/issues",
                    "gh api -XPATCH repos/o/r/issues", "gh api --method=PUT repos/o/r/issues", "gh api -X post /repos/o/r/issues"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("api-write", 2, "api/repos/o/r/issues")])
        self.assertEqual(self.acts("gh api graphql -f query='mutation { addStar(input: {}) { clientMutationId } }'"),
                         [("api-write", 2, "api/graphql")])
        for cmd in ("gh api repos/o/r/pulls", "gh api -X GET repos/o/r/issues -f state=open",
                    "gh api graphql -f query='query { viewer { login } }'", "gh api --paginate repos/o/r/issues"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_gh_writes_to_secrets_workflows_runs_and_repositories_are_api_writes_or_publishing(self):
        for cmd, act in (("gh secret set TOKEN", ("api-write", 2, "secret/TOKEN")),
                         ("gh variable delete X", ("api-write", 2, "variable/X")),
                         ("gh workflow run ci.yml", ("api-write", 2, "workflow/ci.yml")),
                         ("gh run cancel 55", ("api-write", 2, "run/55")), ("gh repo delete o/r --yes", ("api-write", 2, "repo/o/r")),
                         ("gh repo create o/new --public", ("publish", 2, "repo/o/new")),
                         ("gh repo edit o/r --visibility public", ("publish", 2, "repo/o/r")),
                         ("gh gist create a.txt", ("publish", 2, "gist"))):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [act])
        self.assertEqual(self.acts("gh repo edit o/r --description x"), [])

    def test_publishing_names_the_package_and_its_version(self):
        for cmd, target in (("npm publish", "@acme/widget@1.4.0"), ("pnpm publish --access public", "@acme/widget@1.4.0"),
                            ("yarn publish", "@acme/widget@1.4.0"), ("uv publish", "acme-widget@2.0.1"),
                            ("twine upload dist/*", "acme-widget@2.0.1"), ("poetry publish --build", "acme-widget@2.0.1"),
                            ("docker push ghcr.io/acme/widget:1.4", "ghcr.io/acme/widget:1.4"),
                            ("docker buildx build -t ghcr.io/acme/widget:1.4 --push .", "ghcr.io/acme/widget:1.4"),
                            ("./mvnw deploy", "maven"), ("./gradlew publish", "gradle")):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("publish", 2, target)])
        for cmd in ("npm publish --dry-run", "cargo publish --dry-run", "./gradlew publishToMavenLocal", "npm pack",
                    "docker build -t x .", "./mvnw verify"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_deploys_are_tier_2_and_reads_and_dry_runs_are_not(self):
        for cmd in ("kubectl apply -f k8s.yaml", "kubectl --context prod delete pod x", "kubectl rollout restart deploy/x",
                    "helm upgrade app ./chart", "terraform apply -auto-approve", "terraform destroy", "pulumi up --yes",
                    "fly deploy", "vercel --prod", "vercel deploy", "netlify deploy --prod", "firebase deploy",
                    "gcloud run deploy api --source .", "aws s3 sync dist s3://acme-site", "aws s3 rm s3://acme/x",
                    "aws cloudformation deploy --template-file t.yaml --stack-name s"):
            with self.subTest(cmd=cmd):
                found = self.acts(cmd)
                self.assertEqual([(a, t) for a, t, _ in found], [("deploy", 2)])
        for cmd in ("kubectl get pods", "kubectl apply --dry-run=client -f k8s.yaml", "kubectl rollout status deploy/x",
                    "kubectl diff -f k8s.yaml", "helm template ./chart", "terraform plan", "pulumi preview",
                    "aws s3 cp s3://acme/x ./x", "aws s3 ls s3://acme", "vercel env ls", "gcloud run services list"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_curl_wget_and_httpie_write_only_with_a_method_or_data_to_a_host_that_is_not_local(self):
        for cmd in ("curl -X POST https://api.example.com/x", "curl -d a=1 https://api.example.com/x",
                    "curl --data-binary @f https://api.example.com/x", "curl -F f=@a https://api.example.com/x",
                    "curl -T a https://api.example.com/x", "curl --json '{}' https://api.example.com/x",
                    "wget --post-data=a https://api.example.com/x", "wget --method=DELETE https://api.example.com/x",
                    "http POST api.example.com/x a=1", "http api.example.com/x name=a"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("api-write", 2, "api.example.com")])
        for cmd in ("curl https://api.example.com/x", "curl -sSL -o out https://api.example.com/x",
                    "curl -X POST http://localhost:8080/x", "curl -d x http://127.0.0.1:5000/",
                    "curl -G -d q=1 https://api.example.com/x", "wget https://api.example.com/x", "http api.example.com/x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_make_just_task_and_package_scripts_take_the_class_of_their_name(self):
        for cmd, action in (("make release", "release"), ("make deploy-prod", "deploy"), ("just publish", "publish"),
                            ("npm run release", "release"), ("pnpm run deploy:staging", "deploy"), ("yarn ship", "release"),
                            ("task push", "push"), ("./scripts/release.sh 26.11.5", "release"),
                            ("python3 scripts/deploy.py", "deploy"), ("/usr/bin/python3 -u scripts/deploy.py", "deploy"),
                            ("bash scripts/publish-docs.sh", "publish")):
            with self.subTest(cmd=cmd):
                self.assertEqual([a for a, _, _ in self.acts(cmd)], [action])
        for cmd in ("make test", "make", "npm run build", "just lint", "yarn test", "./scripts/check.sh"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    def test_every_shell_form_that_runs_an_action_is_classified(self):
        for cmd in ("bash -c 'gh pr merge 12'", "sh -c \"gh pr merge 12\"", "eval 'gh pr merge 12'",
                    "printf 'gh pr merge 12' | sh", "echo \"gh pr merge 12\" | bash", "bash <<< 'gh pr merge 12'",
                    "printf '%s' 'gh pr merge 12' | bash -s", "ls && echo 'gh pr merge 12' | /bin/sh",
                    "bash -c \"printf 'gh pr merge 12' | sh\"",
                    "x=$(gh pr merge 12)", "echo \"$(gh pr merge 12)\"", "nice -n 5 gh pr merge 12",
                    "timeout 60 gh pr merge 12", "env FOO=1 gh pr merge 12", "cd /tmp && cd - && gh pr merge 12",
                    "if true; then gh pr merge 12; fi", "find . -maxdepth 0 -exec gh pr merge 12 \\;",
                    "python3 -c \"import os; os.system('gh pr merge 12')\"",
                    "python3 -c \"import subprocess; subprocess.run(['gh', 'pr', 'merge', '12'])\"",
                    "node -e \"require('child_process').execSync('gh pr merge 12')\"",
                    "bash -c \"sh -c 'gh pr merge 12'\"", "ssh -p 22 host 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [("merge", 2, "12")])
        self.assertEqual(self.acts("git push origin 'refs/heads/*:refs/heads/*'"),
                         [("push-destructive", 2, "all-branches")])

    def test_text_that_only_carries_an_action_is_not_one(self):
        for cmd in ("git commit -m 'gh pr merge 12'", "echo 'run dev release now'", "grep -rn 'npm publish' docs",
                    "cat > notes.md <<'EOF'\ngh pr merge 12\nEOF", "git push -n", "git push --dry-run origin main",
                    "gh pr merge --help", "dev release --dry-run", "dev push --dry-run",
                    "python3 -c \"print('gh pr merge 12')\"", "dev crew ask 'may I run gh pr merge 12?'",
                    "printf 'gh pr merge 12' > note.txt", "git commit -m \"printf 'gh pr merge 12' | sh\"",
                    "echo \"bash <<< 'gh pr merge 12'\"", "printf 'gh pr merge 12' | grep merge",
                    "printf 'gh pr merge 12' | sh -c 'cat > /dev/null'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])


class MemberTests(GateCase):

    def test_member_merge_through_bash_c_is_denied(self):
        for cmd in ("bash -c 'gh pr merge 12'", "gh pr merge 12", "dev pr merge 12", "printf 'gh pr merge 12' | sh",
                    "eval 'dev release'"):
            with self.subTest(cmd=cmd):
                self.assertIn("never", self.member(cmd) or "")

    def test_member_remote_delete_is_denied(self):
        for cmd in ("git push origin --delete feat/x", "gh api -X DELETE repos/o/r/git/refs/heads/x",
                    "git push origin :feat/x"):
            with self.subTest(cmd=cmd):
                self.assertIn("never", self.member(cmd) or "")

    def test_a_member_never_runs_tier_2_even_with_a_grant_and_the_store_is_not_asked(self):
        store = Store({"merge 12": 1})
        self.assertIn("never", self.member("gh pr merge 12", grant=store) or "")
        self.assertEqual(store.calls, [])
        self.assertEqual(store.left["merge 12"], 1)

    def test_member_push_without_grant_is_denied(self):
        why = self.member("git push origin feat/x", grant=Store())
        self.assertIn("step grant", why or "")
        self.assertIn("--allow push,pr", why or "")
        self.assertIn("step grant", self.member("gh pr create --fill", grant=Store()) or "")

    def test_a_member_push_with_a_step_grant_runs_and_uses_it(self):
        store = Store({"push feat/x": 2, "pr feat/x": 1})
        self.assertIsNone(self.member("git push origin feat/x && gh pr create --fill", grant=store))
        self.assertEqual(store.left, {"push feat/x": 1, "pr feat/x": 0})
        self.assertEqual([c for c in store.calls if c[2]], [("push", "feat/x", True), ("pr", "feat/x", True)])

    def test_a_member_without_a_lane_never_borrows_the_main_sessions_grants(self):
        store = Store({"push feat/x": 5})
        self.assertIn("step grant", self.member("git push", lane=None, grant=store) or "")
        self.assertEqual(store.calls, [])

    def test_a_grant_for_one_branch_does_not_open_another(self):
        store = Store({"push feat/x": 5})
        self.assertIn("step grant", self.member("git push origin feat/y", grant=store) or "")
        self.assertEqual(store.left, {"push feat/x": 5})

    def test_gate_error_denies_member_and_audits_main(self):
        why = self.member("git push origin feat/x", grant=Broken())
        self.assertIn("could not verify", why or "")
        self.records.clear()
        self.assertIsNone(self.main("dev release --version 26.11.5", grant=Broken(), setting="enforce"))
        self.assertIn("guard.failopen", [a[0] for a, _ in self.records])

    def test_a_member_command_the_guard_cannot_parse_that_names_an_action_is_denied(self):
        self.assertIsNotNone(self.member("gh pr merge 12 'unclosed"))
        self.assertIsNone(self.member("ls 'unclosed"))

    def test_a_member_reads_and_builds_as_before(self):
        for cmd in ("git status", "gh pr view 12", "pytest -q", "git commit -m 'gh pr merge 12'", "git push -n"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.member(cmd))

    def test_the_member_setting_never_weakens_on_a_typo(self):
        self.assertIn("never", self.member("gh pr merge 12", setting="off") or "")
        self.assertIsNone(self.member("gh pr merge 12", setting="audit"))
        self.assertEqual(self.gate_records()[-1][1], "audit")

    def test_a_member_never_sends_creates_or_deletes_through_mcp_and_an_unknown_tool_counts_as_a_message(self):
        def v(tool):
            r = guard.member_tool_verdict(tool, {}, self.repo, level="build", member="a1")
            return r[1] if r else None
        for tool in ("mcp__claude_ai_Google_Drive__create_file", "mcp__github__delete_branch",
                     "mcp__claude_ai_Figma__upload_assets", "mcp__acme__frobnicate", "mcp__slack__post_message"):
            with self.subTest(tool=tool):
                self.assertIsNotNone(v(tool))
        for tool in ("mcp__chrome-devtools__click", "mcp__claude_ai_Google_Drive__search_files",
                     "mcp__devkit__devkit_task_add", "mcp__claude_ai_Figma__use_figma"):
            with self.subTest(tool=tool):
                self.assertIsNone(v(tool))


class MainTests(GateCase):

    def test_main_tier_1_runs_and_is_recorded(self):
        self.assertIsNone(self.main("git push origin feat/x"))
        target, outcome, extra = self.gate_records()[-1]
        self.assertEqual((target, outcome, extra["action"], extra["tier"]), ("feat/x", "allow", "push", 1))

    def test_main_release_without_grant_is_denied_in_enforce(self):
        why = self.main("dev release --version 26.11.5", setting="enforce")
        self.assertIn("grant release v26.11.5", why or "")
        self.assertEqual(self.gate_records()[-1][1], "deny")
        self.assertIn("grant merge 12", self.main("bash -c 'gh pr merge 12'") or "")

    def test_main_release_without_grant_runs_with_a_record_in_audit(self):
        self.assertIsNone(self.main("dev release --version 26.11.5", setting="audit"))
        target, outcome, extra = self.gate_records()[-1]
        self.assertEqual((target, outcome, extra["action"], extra["tier"]), ("v26.11.5", "audit", "release", 2))

    def test_release_grant_opens_one_release(self):
        store = Store({"release v26.11.5": 1})
        self.assertIsNone(self.main("dev release --version 26.11.5", grant=store))
        self.assertEqual(self.gate_records()[-1][1], "granted")
        self.assertIn("grant release v26.11.5", self.main("dev release --version 26.11.5", grant=store) or "")

    def test_a_command_with_two_actions_uses_no_grant_unless_both_are_granted(self):
        store = Store({"release v26.11.5": 1})
        self.assertIn("grant publish @acme/widget@1.4.0",
                      self.main("dev release --version 26.11.5 && npm publish", grant=store) or "")
        self.assertEqual(store.left, {"release v26.11.5": 1})

    def test_a_target_known_only_when_the_command_runs_is_never_granted(self):
        for cmd, target in (("ls | xargs -I{} gh pr merge {}", "{}"), ("gh pr merge $PR", "$PR"),
                            ("gh release create \"$(cat VERSION)\"", "$(cat VERSION)")):
            with self.subTest(cmd=cmd):
                store = Store({f"merge {target}": 5, f"release {target}": 5})
                self.assertIn("only known when it runs", self.main(cmd, grant=store) or "")
                self.assertEqual(store.calls, [])
        self.assertFalse(guard.target_matches("merge", "{}", "{}"))
        self.assertFalse(guard.target_matches("release", "$V", "$V"))

    def test_a_grant_lost_between_the_check_and_its_use_denies(self):
        class Racy(Store):
            def __call__(self, action, target, repo, consume):
                return None if consume else {"id": "G1", "source": "E2"}
        self.assertIsNotNone(self.main("gh pr merge 12", grant=Racy()))

    def test_main_mcp_send_and_publish_need_a_grant_in_enforce(self):
        def v(tool, store):
            with settings(main="enforce"):
                r = guard.irreversible_tool_verdict(tool, {}, self.repo, agent="claude", session="s1", grant=store)
            return r[1] if r else None
        self.assertIn("grant external-message mcp__slack__post_message", v("mcp__slack__post_message", Store()) or "")
        self.assertIsNone(v("mcp__slack__post_message", Store({"external-message mcp__slack__post_message": 1})))
        for tool in ("mcp__claude_ai_Google_Drive__search_files", "mcp__devkit__devkit_task_update",
                     "mcp__chrome-devtools__click", "Bash"):
            with self.subTest(tool=tool):
                self.assertIsNone(v(tool, Store()))

    def test_an_unparseable_main_command_that_names_an_action_is_denied_in_enforce_only(self):
        self.assertIsNotNone(self.main("gh pr merge 12 'unclosed", setting="enforce"))
        self.assertIsNone(self.main("gh pr merge 12 'unclosed", setting="audit"))
        self.assertIsNone(self.main("ls 'unclosed", setting="enforce"))


class SettingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(core, "STATE", Path(self.tmp.name) / "state")
        p.start()
        self.addCleanup(p.stop)

    def test_the_main_session_audits_for_its_first_week_then_enforces(self):
        t0 = 1_800_000_000.0
        with settings():
            self.assertEqual(guard.irreversible_setting("main", now=t0), "audit")
            self.assertEqual(guard.irreversible_setting("main", now=t0 + 6 * DAY), "audit")
            self.assertEqual(guard.irreversible_setting("main", now=t0 + 8 * DAY), "enforce")
            self.assertEqual(guard.irreversible_setting("main", now=t0 - DAY), "enforce")  # a start in the future
            self.assertEqual(guard.irreversible_setting("member", now=t0), "enforce")
        with settings(main="enforce"):
            self.assertEqual(guard.irreversible_setting("main", now=t0), "enforce")
        with settings(main="off", member="disabled"):
            self.assertEqual(guard.irreversible_setting("main", now=t0), "enforce")
            self.assertEqual(guard.irreversible_setting("member", now=t0), "enforce")

    def test_an_unreadable_start_record_enforces(self):
        (core.STATE).mkdir(parents=True)
        (core.STATE / "irreversible-gate.json").write_text("{not json")
        with settings():
            self.assertEqual(guard.irreversible_setting("main", now=1_800_000_000.0), "enforce")


class GrantRuleTests(unittest.TestCase):

    def test_targets_match_exactly_tags_with_or_without_v_and_push_globs_never_the_default_branch(self):
        self.assertTrue(guard.target_matches("release", "v26.11.4", "26.11.4"))
        self.assertTrue(guard.target_matches("release", "26.11.4", "v26.11.4"))
        self.assertFalse(guard.target_matches("release", "v26.11.4", "v26.11.5"))
        self.assertTrue(guard.target_matches("push", "feat/*", "feat/x"))
        self.assertFalse(guard.target_matches("push", "feat/*", "fix/x"))
        for default in ("main", "master"):
            self.assertFalse(guard.target_matches("push", "*", default))
        self.assertFalse(guard.target_matches("push", "trunk*", "trunk", default="trunk"))
        self.assertTrue(guard.target_matches("merge", "12", "12"))
        self.assertFalse(guard.target_matches("merge", "1*", "12"))
        self.assertFalse(guard.target_matches("publish", "@acme/*", "@acme/widget@1.4.0"))
        self.assertFalse(guard.target_matches("push", "", "feat/x"))
        self.assertFalse(guard.target_matches("push", "feat/x", ""))

    def test_a_preapproval_names_one_narrow_target_and_never_covers_publishing_or_deploying(self):
        for action, target in (("push", ""), ("push", "main"), ("push", "*"), ("push", "**"), ("push", "?*"),
                               ("push-destructive", "master"), ("publish", "@acme/widget@1.4.0"), ("deploy", "fly"),
                               ("release", "v26.11.*"), ("merge", "1*"), ("frobnicate", "x"), ("release", "")):
            with self.subTest(action=action, target=target):
                self.assertIsNotNone(guard.preapproval_problem(action, target))
        for action, target in (("push", "feat/*"), ("push", "fix/login-timeout"), ("release", "v26.11.4"),
                               ("merge", "12"), ("pr", "feat/x")):
            with self.subTest(action=action, target=target):
                self.assertIsNone(guard.preapproval_problem(action, target))


if __name__ == "__main__":
    unittest.main()
