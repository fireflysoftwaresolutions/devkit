# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The registry workflow: it folds every payload comment of a registry issue into the one record comment its bot
writes, keeps the abuse limits, moves a duplicate's votes to the first issue, marks bugs fixed when a release is
published, and reopens regressions. A fake GitHub stands in for the API, the events are GitHub's own JSON shapes
written to a file as the runner does, and the workflow file is checked for its hardening."""
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import bugs  # noqa: E402

_spec = importlib.util.spec_from_file_location("bug_registry_action", ROOT / "tools" / "bug_registry_action.py")
action = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(action)

WORKFLOW = ROOT / ".github" / "workflows" / "bug-registry.yml"
KEY = "5c" * 32
FP = bugs.fingerprint("crash", exc_class="KeyError", frames=("work.actor_str",))  # what the report fixture hashes
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
DAY = 86400.0
TODAY = "2027-01-15"
BOT = {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _user(n: int) -> dict:
    return {"login": f"user{n}", "id": n, "type": "User"}


def _h(account_id: int, key: str = KEY) -> str:
    return hmac.new(key.encode(), str(account_id).encode(), hashlib.sha256).hexdigest()


def _payload(kind, fp=FP, **fields):
    base = {"devkit_bug": 1, "type": kind, "fingerprint": fp, "version": "26.11.7"}
    if kind == "report":
        base.update({"impact": "blocked", "kind": "crash", "command": "progress step", "exc_class": "KeyError",
                     "frames": ["work.actor_str"]})
    elif kind == "vote":
        base.update({"impact": "blocked", "window": TODAY})
    elif kind == "detour":
        base.update({"steps": [{"dev": "sync --offline"}], "evidence": {"successes": 1}})
    elif kind in ("confirm", "failed"):
        base.update({"detour_id": "d1"})
    base.update(fields)
    return base


def _body(payload) -> str:
    return bugs.render_payload(payload)


# GitHub's event payloads, trimmed to the fields the workflow reads; each test fills in the issue and comment it is
# about and writes the event to a file, as the runner does.
EVENTS = {
    "issues": {
        "action": "opened",
        "issue": {"number": 12, "title": "[bug] crash in dev progress step: KeyError", "body": "",
                  "user": {"login": "user1", "id": 1, "type": "User"}, "author_association": "NONE", "labels": [],
                  "state": "open", "state_reason": None, "created_at": "2027-01-15T07:00:00Z", "closed_at": None},
        "repository": {"full_name": "owner/registry", "default_branch": "main"},
        "sender": {"login": "user1", "id": 1, "type": "User"},
    },
    "issue_comment": {
        "action": "created",
        "issue": {"number": 12, "title": "[bug] crash in dev progress step: KeyError", "body": "",
                  "user": {"login": "user1", "id": 1, "type": "User"}, "author_association": "NONE",
                  "labels": [{"name": "devkit-bug"}, {"name": "kind:crash"}], "state": "open"},
        "comment": {"id": 1001, "body": "", "user": {"login": "user2", "id": 2, "type": "User"},
                    "author_association": "NONE", "created_at": "2027-01-15T07:59:00Z"},
        "repository": {"full_name": "owner/registry", "default_branch": "main"},
        "sender": {"login": "user2", "id": 2, "type": "User"},
    },
    "release": {
        "action": "published",
        "release": {"id": 2, "tag_name": "v26.11.8", "name": "26.11.8", "draft": False, "prerelease": False,
                    "created_at": "2027-01-15T07:55:00Z", "published_at": "2027-01-15T08:00:00Z"},
        "repository": {"full_name": "owner/registry", "default_branch": "main"},
        "sender": {"login": "maint", "id": 9, "type": "User"},
    },
}
REPORTED = "Steps close but the board still shows them running"  # a reporter's own words, in a command report


class FakeGitHub:
    """An in-memory registry repository behind the same methods as the workflow's GitHub client. Comments come back
    newest first, so the workflow has to put them in order itself."""

    def __init__(self):
        self.repo = "owner/registry"
        self.issues, self.notes = {}, {}
        self.next_id = 1000
        self.members, self.closers, self.release_list = set(), {}, []
        self.calls, self.writes = [], []
        self.on_reread = None  # plays another writer: changes a comment right before the workflow re-reads it
        self.after_record_write = None  # plays the reporter: edits the issue right after the bot writes its record
        self.max_body = 65536  # GitHub refuses a longer comment with HTTP 422

    # ---- set-up
    def add_issue(self, number, body, user, assoc="NONE", **fields):
        self.issues[number] = {"number": number, "title": "[bug] a title someone typed", "body": body, "user": user,
                               "author_association": assoc, "labels": [], "state": "open", "state_reason": None,
                               "created_at": _iso(NOW - 3600), "updated_at": _iso(NOW - 3600), "closed_at": None,
                               **fields}

    def add_comment(self, number, body, user, assoc="NONE", ts=NOW - 60):
        self.next_id += 1
        self.notes[self.next_id] = {"id": self.next_id, "issue": number, "body": body, "user": dict(user),
                                    "author_association": assoc, "created_at": _iso(ts), "updated_at": _iso(ts),
                                    "issue_url": f"https://api.github.com/repos/{self.repo}/issues/{number}"}
        return self.next_id

    def registry_issue(self, number, opener=1, rec_ts=NOW - DAY, **record):
        """A registry issue whose first run already wrote the record: a report from `opener`, labels, the bot's
        record comment."""
        self.add_issue(number, _body(_payload("report")), _user(opener), labels=["devkit-bug", "kind:crash"])
        rec = bugs.fold({}, _payload("report"), author_hash=_h(opener), author_assoc="NONE", now=rec_ts)
        rec.update(record)
        cid = self.add_comment(number, "", BOT, ts=rec_ts)
        rec["seen"] = cid
        self.notes[cid]["body"] = bugs.render_record(rec)
        return cid

    def record(self, number):
        held = [c for c in sorted(self.notes.values(), key=lambda c: c["id"])
                if c["issue"] == number and c["user"]["login"] == BOT["login"] and bugs.RECORD_MARK in c["body"]]
        if len(held) > 1:
            raise AssertionError(f"issue {number} has {len(held)} record comments")
        return bugs.parse_record(held[0]["body"]) if held else None

    def record_comment(self, number):
        return next(c for c in self.notes.values()
                    if c["issue"] == number and c["user"]["login"] == BOT["login"] and bugs.RECORD_MARK in c["body"])

    def _wrote_record(self, body):
        if bugs.RECORD_MARK in body and self.after_record_write:
            hook, self.after_record_write = self.after_record_write, None
            hook(self)

    def ids(self, number):
        return {c["id"] for c in self.notes.values() if c["issue"] == number}

    # ---- the client interface
    def _issue_view(self, i):
        out = copy.deepcopy(i)
        out["labels"] = [{"name": n} for n in i["labels"]]
        return out

    def issue(self, number):
        self.calls.append(("issue", number))
        return self._issue_view(self.issues[number])

    def comments(self, number):
        self.calls.append(("comments", number))
        return copy.deepcopy(sorted((c for c in self.notes.values() if c["issue"] == number), key=lambda c: -c["id"]))

    def comment(self, cid):
        self.calls.append(("comment", cid))
        if self.on_reread:
            hook, self.on_reread = self.on_reread, None
            hook(self)
        return copy.deepcopy(self.notes[cid])

    def _check_size(self, body):
        if self.max_body is not None and len(body) > self.max_body:
            raise action.GitHubError(f"comment: HTTP 422 body is too long ({len(body)})", status=422)

    def create_comment(self, number, body):
        self._check_size(body)
        self.writes.append(("create_comment", number))
        cid = self.add_comment(number, body, BOT, ts=NOW)
        self._wrote_record(body)
        return {"id": cid}

    def edit_comment(self, cid, body):
        self._check_size(body)
        self.writes.append(("edit_comment", cid))
        self.notes[cid]["body"] = body
        self.notes[cid]["updated_at"] = _iso(NOW)
        self._wrote_record(body)

    def delete_comment(self, cid):
        self.writes.append(("delete_comment", cid))
        self.notes.pop(cid, None)

    def add_labels(self, number, labels):
        self.writes.append(("add_labels", number))
        for name in labels:
            if name not in self.issues[number]["labels"]:
                self.issues[number]["labels"].append(name)

    def edit_issue(self, number, **fields):
        self.writes.append(("edit_issue", number))
        self.issues[number].update(fields)

    def updated_comments(self, since):
        self.calls.append(("updated_comments", since))
        return copy.deepcopy([c for c in self.notes.values() if c["updated_at"] >= since])

    def find(self, fp):
        self.calls.append(("find", fp))
        return [n for n, i in self.issues.items() if fp in (i["body"] or "") and "devkit-bug" in i["labels"]]

    def recent_issues(self):
        self.calls.append(("recent_issues",))
        return [self._issue_view(i) for _, i in sorted(self.issues.items(), reverse=True)]

    def releases(self):
        self.calls.append(("releases",))
        return copy.deepcopy(self.release_list)

    def closed_issues(self, label, since):
        self.calls.append(("closed_issues", label, since))
        return [self._issue_view(i) for i in self.issues.values()
                if i["state"] == "closed" and label in i["labels"] and (not since or i["updated_at"] >= since)]

    def closure(self, number):
        self.calls.append(("closure", number))
        return dict(self.closers.get(number) or {"actor": "", "actor_id": 0, "merged_pr": False})

    def is_member(self, login):
        self.calls.append(("is_member", login))
        return login in self.members


def _run(fake, event_name, number=None, comment=None, key=KEY, sleep=None, maintainers=None, **release):
    """Run the workflow's script on one event, as the runner would: the event JSON in a file, the secret in the
    environment. `maintainers` is the maintainers file's text, a path to read instead, or None for no file, so the
    repository's own list never changes what a test sees. -> (exit code, stdout, stderr)."""
    event = copy.deepcopy(EVENTS[event_name])
    if number is not None:
        event["issue"] = fake.issue(number)
        fake.calls.clear()
    if comment is not None:
        event["comment"] = copy.deepcopy(fake.notes[comment])
    if event_name == "release":
        event["release"].update(release)
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        listed = maintainers if isinstance(maintainers, Path) else Path(d) / "maintainers"
        if isinstance(maintainers, str):
            listed.write_text(maintainers, encoding="utf-8")
        with mock.patch.dict(os.environ, {"BUG_REGISTRY_KEY": key or ""}), redirect_stdout(out), \
                redirect_stderr(err):
            if key is None:
                del os.environ["BUG_REGISTRY_KEY"]
            rc = action.main(path, event_name, fake, now=NOW, sleep=sleep or (lambda s: None), maintainers=listed)
    return rc, out.getvalue(), err.getvalue()


class FoldTests(unittest.TestCase):
    def test_folds_every_unfolded_payload_in_comment_id_order(self):
        fake = FakeGitHub()
        record_id = fake.registry_issue(12)
        fake.add_comment(12, _body(_payload("detour")), _user(2), "MEMBER")
        fake.add_comment(12, _body(_payload("confirm")), _user(3), "MEMBER")  # needs the detour folded first
        fake.add_comment(12, _body(_payload("vote")), _user(4))
        last = fake.add_comment(12, _body(_payload("vote", impact="detoured")), _user(5))
        rc, _, err = _run(fake, "issue_comment", 12, comment=last)  # one event for four comments: one run was cancelled
        self.assertEqual(rc, 0, err)
        rec = fake.record(12)
        self.assertEqual(set(rec["voters"]), {_h(1), _h(4), _h(5)})
        self.assertEqual([(d["id"], d["trust"]) for d in rec["detours"]], [("d1", "verified")])
        self.assertEqual(rec["seen"], last)
        self.assertEqual(fake.record_comment(12)["id"], record_id)  # the same comment, rewritten

    def test_invalid_payloads_are_ignored_and_left_alone(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        bad = {fake.add_comment(12, _body(_payload("vote", extra=1)), _user(2)),
               fake.add_comment(12, _body(_payload("vote", impact="catastrophic")), _user(3)),
               fake.add_comment(12, _body(_payload("vote", fp="aaaaaaaaaaaa")), _user(4)),
               fake.add_comment(12, "Thanks, same here on 26.11.7.", _user(5))}
        good = fake.add_comment(12, _body(_payload("vote")), _user(6))
        rc, _, err = _run(fake, "issue_comment", 12, comment=good)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.record(12)["voters"]), {_h(1), _h(6)})
        self.assertTrue(bad <= fake.ids(12))
        self.assertNotIn(good, fake.ids(12))

    def test_pull_request_events_are_skipped(self):
        fake = FakeGitHub()
        fake.add_issue(30, _body(_payload("report")), _user(3), pull_request={"url": "x", "merged_at": None})
        cid = fake.add_comment(30, _body(_payload("vote")), _user(3))
        rc, _, _ = _run(fake, "issue_comment", 30, comment=cid)
        self.assertEqual(rc, 0)
        self.assertEqual((fake.calls, fake.writes), ([], []))

    def test_a_title_comes_only_from_ids_and_class_names_devkit_makes(self):
        # a stranger's report names a refusal the guard never makes, or a token where an exception class goes: the
        # issue is left alone, and its title never carries that text
        token = "ghp_" + "A1b2C3d4E5f6" * 3
        def guard(rid):
            return {**_payload("report", command="", exc_class="", frames=[], word="git", refusal_id=rid),
                    "kind": "guard", "fingerprint": bugs.fingerprint("guard", refusal_id=rid, word="git")}
        made_up = {50: guard("acme:payments-core"), 51: _payload("report", exc_class=token)}
        for number, payload in made_up.items():
            fake = FakeGitHub()
            fake.add_issue(number, _body(payload), _user(7))
            rc, _, err = _run(fake, "issues", number)
            self.assertEqual(rc, 0, err)
            self.assertEqual((fake.issues[number]["labels"], fake.record(number)), ([], None))
            self.assertEqual(fake.issues[number]["title"], "[bug] a title someone typed")
        fake = FakeGitHub()
        fake.add_issue(52, _body(guard("check_git:force-push")), _user(7))
        rc, _, err = _run(fake, "issues", 52)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.issues[52]["title"], "Guard refuses a plain git push --force")

    def test_labels_and_record_follow_only_a_valid_first_payload(self):
        fake = FakeGitHub()
        fake.add_issue(40, _body(_payload("report")), _user(7))
        fake.issues[40]["title"] = "[bug] crash while working on payments-core"
        rc, _, err = _run(fake, "issues", 40)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.issues[40]["labels"]), {"devkit-bug", "kind:crash"})
        self.assertEqual(set(fake.record(40)["voters"]), {_h(7)})
        self.assertEqual(fake.issues[40]["title"], "dev progress step crashes with KeyError")

        fake.add_issue(41, _body(_payload("report", extra="field")), _user(8))
        rc, _, _ = _run(fake, "issues", 41)
        later = fake.add_comment(41, _body(_payload("vote")), _user(9))
        rc2, _, _ = _run(fake, "issue_comment", 41, comment=later)
        self.assertEqual((rc, rc2), (0, 0))
        self.assertEqual(fake.issues[41]["labels"], [])
        self.assertIsNone(fake.record(41))
        self.assertFalse([w for w in fake.writes if w[1] in (41, later)])

        fake.add_issue(42, "Please look at this crash.", _user(8), labels=["devkit-bug"])  # labeled by a person
        chat = fake.add_comment(42, _body(_payload("vote")), _user(9))
        rc, _, err = _run(fake, "issue_comment", 42, comment=chat)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(fake.record(42))
        self.assertIn(chat, fake.ids(42))

    def test_duplicate_issue_is_closed_and_its_votes_moved(self):
        fake = FakeGitHub()
        fake.registry_issue(7)
        fake.add_issue(9, _body(_payload("report")), _user(2))
        vote = fake.add_comment(9, _body(_payload("vote")), _user(3))
        rc, _, err = _run(fake, "issues", 9)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.record(7)["voters"]), {_h(1), _h(2), _h(3)})
        self.assertEqual((fake.issues[9]["state"], fake.issues[9]["state_reason"]), ("closed", "not_planned"))
        self.assertEqual(fake.issues[9]["labels"], [])
        self.assertNotIn(vote, fake.ids(9))
        self.assertTrue(any(c["body"].startswith("Duplicate of #7") for c in fake.notes.values() if c["issue"] == 9))
        self.assertEqual((fake.record(9)["status"], fake.record(9)["duplicate_of"]), ("duplicate", 7))

        late = fake.add_comment(9, _body(_payload("vote")), _user(4))  # a client that still posts to the duplicate
        rc, _, err = _run(fake, "issue_comment", 9, comment=late)
        self.assertEqual(rc, 0, err)
        self.assertIn(_h(4), fake.record(7)["voters"])
        self.assertNotIn(late, fake.ids(9))

    def test_duplicate_waits_for_a_first_issue_still_being_written(self):
        fake = FakeGitHub()
        fake.add_issue(7, _body(_payload("report")), _user(1))  # its own first run has not written the record yet
        fake.add_issue(9, _body(_payload("report")), _user(2))
        waits = []

        def first_run_finishes(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.assertEqual(_run(fake, "issues", 7)[0], 0)
        rc, _, err = _run(fake, "issues", 9, sleep=first_run_finishes)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(waits), 1)
        self.assertEqual(set(fake.record(7)["voters"]), {_h(1), _h(2)})

        never = FakeGitHub()
        never.add_issue(7, _body(_payload("report")), _user(1))
        never.add_issue(9, _body(_payload("report")), _user(2))
        rc, _, err = _run(never, "issues", 9)
        self.assertNotEqual(rc, 0)
        self.assertIn("#7", err)
        self.assertEqual(never.writes, [])

    def test_daily_cap_counts_each_account_across_the_registry(self):
        fake = FakeGitHub()
        fake.registry_issue(50)
        fake.registry_issue(51, rec_ts=NOW - 600, folds={"day": TODAY, "by": {_h(8): 19}})
        fake.registry_issue(52, rec_ts=NOW - 600, folds={"day": "2027-01-14", "by": {_h(9): 50}})
        vote = fake.add_comment(50, _body(_payload("vote")), _user(8))
        over = {fake.add_comment(50, _body(_payload("detour")), _user(8)),
                fake.add_comment(50, _body(_payload("vote", impact="minor")), _user(8))}
        other = fake.add_comment(50, _body(_payload("vote")), _user(9))
        rc, _, err = _run(fake, "issue_comment", 50, comment=other)
        self.assertEqual(rc, 0, err)
        rec = fake.record(50)
        self.assertEqual(set(rec["voters"]), {_h(1), _h(8), _h(9)})
        self.assertEqual(rec["voters"][_h(8)]["impact"], "blocked")  # the minor vote past the cap was not folded
        self.assertEqual(rec["detours"], [])
        self.assertEqual(rec["folds"], {"day": TODAY, "by": {_h(8): 1, _h(9): 1}})
        self.assertNotIn(vote, fake.ids(50))
        self.assertTrue(over <= fake.ids(50))

    def test_folded_votes_confirms_and_failures_are_deleted_detours_and_reports_kept(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        fake.add_comment(12, _body(_payload("detour")), _user(2))
        first = fake.add_comment(12, _body(_payload("vote")), _user(3))
        rc, _, err = _run(fake, "issue_comment", 12, comment=first)
        self.assertEqual(rc, 0, err)
        gone = {fake.add_comment(12, _body(_payload("confirm")), _user(5), "MEMBER"),
                fake.add_comment(12, _body(_payload("failed", impact="blocked")), _user(6))}
        kept = {fake.add_comment(12, _body(_payload("detour", steps=[{"hint": "scratch-tool", "tool": "Write"}])),
                                 _user(4)),
                fake.add_comment(12, _body(_payload("report")), _user(7))}
        rc, _, err = _run(fake, "issue_comment", 12, comment=max(kept))
        self.assertEqual(rc, 0, err)
        ids = fake.ids(12)
        self.assertNotIn(first, ids)
        self.assertFalse(gone & ids)
        self.assertTrue(kept <= ids)
        d1, d2 = fake.record(12)["detours"]
        self.assertEqual((len(d1["confirmations"]), len(d1["failures"]), d1["flagged"]), (1, 1, True))
        self.assertEqual(d2["steps"], [{"hint": "scratch-tool", "tool": "Write"}])
        self.assertIn(_h(7), fake.record(12)["voters"])

    def test_a_folded_payload_is_never_folded_again(self):
        fake = FakeGitHub()
        fake.registry_issue(12, rec_ts=NOW - 600)
        detour = fake.add_comment(12, _body(_payload("detour")), _user(2))
        report = fake.add_comment(12, _body(_payload("report")), _user(3))
        self.assertEqual(_run(fake, "issue_comment", 12, comment=report)[0], 0)
        chat = fake.add_comment(12, "Still happens.", _user(4))
        self.assertEqual(_run(fake, "issue_comment", 12, comment=chat)[0], 0)
        self.assertTrue({detour, report} <= fake.ids(12))  # kept on the issue, and counted once
        self.assertEqual(fake.record(12)["folds"], {"day": TODAY, "by": {_h(2): 1, _h(3): 1}})

    def test_voters_are_keyed_hashes_of_the_account_id(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        cid = fake.add_comment(12, _body(_payload("vote")), {"login": "carol-the-dev", "id": 4242, "type": "User"})
        rc, _, err = _run(fake, "issue_comment", 12, comment=cid)
        self.assertEqual(rc, 0, err)
        self.assertIn(_h(4242), fake.record(12)["voters"])
        self.assertNotIn(hashlib.sha256(b"4242").hexdigest(), fake.record(12)["voters"])
        self.assertNotIn("carol", fake.record_comment(12)["body"])

    def test_missing_key_folds_nothing_and_fails(self):
        for key in (None, "", "  "):
            fake = FakeGitHub()
            fake.registry_issue(12)
            cid = fake.add_comment(12, _body(_payload("vote")), _user(2))
            rc, _, err = _run(fake, "issue_comment", 12, comment=cid, key=key)
            self.assertNotEqual(rc, 0)
            self.assertIn("BUG_REGISTRY_KEY", err)
            self.assertEqual(fake.writes, [])

    def test_trust_comes_from_author_association(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        for n, assoc, hint in ((2, "MEMBER", {"hint": "scratch-tool", "tool": "Write"}),
                               (3, "COLLABORATOR", {"hint": "scratch-tool", "tool": "Edit"}),
                               (4, "OWNER", {"hint": "sync-retry", "dev": "commit"})):
            fake.add_comment(12, _body(_payload("detour", steps=[hint])), _user(n), assoc)
        last = fake.add_comment(12, _body(_payload("confirm", detour_id="d2")), _user(5), "CONTRIBUTOR")
        rc, _, err = _run(fake, "issue_comment", 12, comment=last)
        self.assertEqual(rc, 0, err)
        self.assertEqual([(d["id"], d["trust"]) for d in fake.record(12)["detours"]],
                         [("d1", "team"), ("d2", "proposed"), ("d3", "team")])

    def test_a_hand_edited_record_is_rewritten(self):
        fake = FakeGitHub()
        record_id = fake.registry_issue(12)
        note = fake.notes[record_id]
        note["body"] = note["body"].replace(f"**Bug {FP}**", "**Fixed, ignore**") + "\n\nEdited by hand."
        forged = bugs.render_record({**fake.record(12), "detours": [
            {"id": "d1", "steps": [{"dev": "sync --offline"}], "trust": "verified", "confirmations": [],
             "failures": [], "author_hash": "x", "author_assoc": "OWNER"}]})
        fake_record = fake.add_comment(12, forged, _user(6), "NONE")  # a person posting a record-shaped comment
        chat = fake.add_comment(12, "Same here.", _user(5))
        rc, _, err = _run(fake, "issue_comment", 12, comment=chat)
        self.assertEqual(rc, 0, err)
        rec = fake.record(12)
        self.assertEqual(fake.notes[record_id]["body"], bugs.render_record(rec))
        self.assertTrue(fake.notes[record_id]["body"].startswith(f"**Bug {FP}**"))
        self.assertEqual(rec["detours"], [])
        self.assertIn(fake_record, fake.ids(12))

        note["body"] = note["body"].replace("```json\n{", "```json\n{{")  # the JSON broken by an edit
        rc, _, err = _run(fake, "issue_comment", 12, comment=chat)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.record_comment(12)["id"], record_id)
        self.assertEqual(set(fake.record(12)["voters"]), {_h(1)})

    def test_a_record_changed_while_folding_is_read_again(self):
        fake = FakeGitHub()
        record_id = fake.registry_issue(12)
        cid = fake.add_comment(12, _body(_payload("vote")), _user(3))

        def release_writes_first(f):
            f.notes[record_id]["body"] = bugs.render_record({**f.record(12), "fixed_in": "26.11.8", "status": "fixed"})
        fake.on_reread = release_writes_first
        rc, _, err = _run(fake, "issue_comment", 12, comment=cid)
        self.assertEqual(rc, 0, err)
        rec = fake.record(12)
        self.assertEqual((rec["fixed_in"], rec["status"]), ("26.11.8", "fixed"))
        self.assertIn(_h(3), rec["voters"])


class HostileInputTests(unittest.TestCase):
    """What a stranger can post: no comment or issue stops the registry from folding the others."""

    def test_a_malformed_payload_is_left_alone_and_the_fold_goes_on(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        deep = '{"devkit_bug":1,"type":"vote","fingerprint":"%s","x":%s%s}' % (FP, "[" * 32000, "]" * 32000)
        left = {fake.add_comment(12, _body(_payload("vote", impact=[])), _user(66)),
                fake.add_comment(12, _body(_payload("detour", evidence={"tools": [{}]})), _user(66)),
                fake.add_comment(12, _body(_payload("report", surface=[], event={})), _user(66)),
                fake.add_comment(12, f"```json\n{deep}\n```", _user(66))}
        vote = fake.add_comment(12, _body(_payload("vote")), _user(22), "MEMBER")
        rc, _, err = _run(fake, "issue_comment", 12, comment=vote)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.record(12)["voters"]), {_h(1), _h(22)})
        self.assertTrue(left <= fake.ids(12))
        chat = fake.add_comment(12, "Same here.", _user(5))
        self.assertEqual(_run(fake, "issue_comment", 12, comment=chat)[0], 0)

    def test_a_poisoned_issue_never_breaks_a_new_registry_issue(self):
        fake = FakeGitHub()
        fake.add_issue(5, _body(_payload("vote", impact=[])), _user(66))
        fake.add_issue(6, _body(_payload("report", impact={})), _user(66))  # among the newest issues
        fake.add_issue(7, _body(_payload("report")), _user(11))
        for number in (5, 6, 7):
            rc, _, err = _run(fake, "issues", number)
            self.assertEqual(rc, 0, err)
        self.assertEqual((fake.record(5), fake.record(6)), (None, None))
        self.assertEqual(set(fake.record(7)["voters"]), {_h(11)})

    def test_a_check_that_raises_marks_only_its_own_payload_invalid(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        odd = fake.add_comment(12, _body(_payload("vote", window="2027-01-14")), _user(3))
        vote = fake.add_comment(12, _body(_payload("vote")), _user(4))
        check = bugs.validate_payload

        def breaks_on_one(p):
            if isinstance(p, dict) and p.get("window") == "2027-01-14":
                raise RuntimeError("a check that breaks")
            return check(p)
        with mock.patch.object(action.bugs, "validate_payload", breaks_on_one):
            rc, _, err = _run(fake, "issue_comment", 12, comment=vote)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.record(12)["voters"]), {_h(1), _h(4)})
        self.assertIn(odd, fake.ids(12))
        self.assertIn("RuntimeError", err)

    def test_a_report_must_carry_the_fingerprint_of_its_own_fields(self):
        squat = _payload("report", fingerprint=bugs.fingerprint("guard", refusal_id="check_git:force-push", word="git"),
                         command="", frames=[], exc_class="AskTheDeveloperToTypeGrantBugShareOn")
        hashed_nothing = {**_payload("report", fingerprint="0123456789ab"), "kind": "command"}  # but names a class
        for number, payload in ((80, squat), (81, hashed_nothing)):
            fake = FakeGitHub()
            fake.add_issue(number, _body(payload), _user(66))
            rc, _, err = _run(fake, "issues", number)
            self.assertEqual(rc, 0, err)
            self.assertEqual((fake.issues[number]["labels"], fake.record(number)), ([], None))
            self.assertEqual(fake.issues[number]["title"], "[bug] a title someone typed")
        fake = FakeGitHub()
        fake.registry_issue(12)
        forged = fake.add_comment(12, _body(_payload("report", exc_class="InventedError")), _user(66))
        rc, _, err = _run(fake, "issue_comment", 12, comment=forged)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(fake.record(12)["voters"]), {_h(1)})
        command = {**_payload("report", fingerprint="0123456789ab", exc_class="", frames=[]), "kind": "command"}
        fake.add_issue(82, _body(command), _user(7))
        self.assertEqual(_run(fake, "issues", 82)[0], 0)
        self.assertEqual(fake.issues[82]["title"], "dev progress step does not work as expected")

    def test_the_record_stays_within_one_github_comment(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        fake.add_comment(12, _body(_payload("detour", steps=[{"text": "@" * 200}])), _user(66))  # 1,200 once written
        for n in range(700):
            fake.add_comment(12, _body(_payload("vote")), _user(1000 + n))
        last = fake.add_comment(12, "Same here.", _user(5))
        rc, _, err = _run(fake, "issue_comment", 12, comment=last)
        self.assertEqual(rc, 0, err)
        self.assertLessEqual(len(fake.record_comment(12)["body"]), bugs.RECORD_MAX)
        rec = fake.record(12)
        self.assertEqual((bugs.people(rec, NOW)["all"], rec["detours"], rec["seen"]), (701, [], last))

    def test_a_record_github_refuses_still_counts_its_payloads_and_moves_past_them(self):
        fake = FakeGitHub()
        record_id = fake.registry_issue(12)
        fake.max_body = len(fake.notes[record_id]["body"]) + 300  # room for the counts, not for a detour
        steps = [{"text": f"Run it again from a fresh clone, step {k}. " + "x" * 150} for k in range(5)]
        big = fake.add_comment(12, _body(_payload("detour", steps=steps)), _user(3))
        rc, _, err = _run(fake, "issue_comment", 12, comment=big)
        self.assertEqual(rc, 1)
        self.assertIn("#12", err)
        rec = fake.record(12)
        self.assertEqual((rec["seen"], rec["detours"], rec["folds"]), (big, [], {"day": TODAY, "by": {_h(3): 1}}))
        self.assertIn(big, fake.ids(12))
        fake.max_body = 65536
        chat = fake.add_comment(12, "Same here.", _user(5))
        self.assertEqual(_run(fake, "issue_comment", 12, comment=chat)[0], 0)
        self.assertEqual(fake.record(12)["folds"], {"day": TODAY, "by": {_h(3): 1}})  # never folded again


def _fixed(number):
    """A registry issue for a bug fixed in 26.11.8, closed as completed."""
    fake = FakeGitHub()
    fake.registry_issue(number, fixed_in="26.11.8", status="fixed")
    fake.issues[number].update(state="closed", state_reason="completed", closed_at=_iso(NOW - 5 * DAY))
    return fake


def _released():
    """A registry at the 26.11.8 release, with bugs closed in every way that counts or does not; `maint` (account 9)
    is a public member, `outsider` (account 41) is not."""
    fake = FakeGitHub()
    fake.release_list = [
        {"id": 1, "tag_name": "v26.11.7", "draft": False, "prerelease": False,
         "published_at": _iso(NOW - 10 * DAY)},
        {"id": 3, "tag_name": "v26.11.8-rc.1", "draft": False, "prerelease": True,
         "published_at": _iso(NOW - 3 * DAY)},
        {"id": 2, "tag_name": "v26.11.8", "draft": False, "prerelease": False, "published_at": _iso(NOW)},
    ]
    fake.members = {"maint"}
    cases = {60: ("completed", 2, "outsider", True), 61: ("completed", 2, "maint", False),
             62: ("completed", 2, "outsider", False), 63: ("not_planned", 2, "maint", False),
             64: ("completed", 20, "maint", True)}
    for number, (reason, days_ago, actor, merged) in cases.items():
        fake.registry_issue(number, rec_ts=NOW - 30 * DAY)
        # a comment after closing moves updated_at, which the API's `since` filters on; closed_at decides
        fake.issues[number].update(state="closed", state_reason=reason, closed_at=_iso(NOW - days_ago * DAY),
                                   updated_at=_iso(NOW - DAY))
        fake.closers[number] = {"actor": actor, "actor_id": {"maint": 9, "outsider": 41}[actor], "merged_pr": merged}
    return fake


class RegressionTests(unittest.TestCase):
    def test_three_accounts_reopen_a_fixed_bug(self):
        fake = _fixed(70)
        for n in (2, 3):
            cid = fake.add_comment(70, _body(_payload("vote", version="26.11.9")), _user(n))
        self.assertEqual(_run(fake, "issue_comment", 70, comment=cid)[0], 0)
        self.assertEqual(fake.issues[70]["state"], "closed")
        self.assertNotIn("regression", fake.issues[70]["labels"])
        self.assertEqual(fake.record(70)["status"], "fixed")
        cid = fake.add_comment(70, _body(_payload("vote", version="26.11.9")), _user(4))
        self.assertEqual(_run(fake, "issue_comment", 70, comment=cid)[0], 0)
        self.assertEqual(fake.issues[70]["state"], "open")
        self.assertIn("regression", fake.issues[70]["labels"])
        self.assertEqual(fake.record(70)["status"], "regressed")

    def test_one_member_reopens_a_fixed_bug(self):
        fake = _fixed(71)
        cid = fake.add_comment(71, _body(_payload("vote", version="26.11.9")), _user(5), "MEMBER")
        self.assertEqual(_run(fake, "issue_comment", 71, comment=cid)[0], 0)
        self.assertEqual(fake.issues[71]["state"], "open")
        self.assertIn("regression", fake.issues[71]["labels"])


class ReleaseTests(unittest.TestCase):
    def test_fixed_in_only_for_bugs_closed_as_completed_by_a_merged_pr_or_a_member(self):
        fake = _released()
        rc, _, err = _run(fake, "release")
        self.assertEqual(rc, 0, err)
        fixed = {n: (fake.record(n)["fixed_in"], fake.record(n)["status"]) for n in (60, 61, 62, 63, 64)}
        self.assertEqual(fixed, {60: ("26.11.8", "fixed"), 61: ("26.11.8", "fixed"), 62: ("", "open"),
                                 63: ("", "open"), 64: ("", "open")})

    def test_a_prerelease_marks_nothing(self):
        fake = _released()
        rc, _, err = _run(fake, "release", tag_name="v26.11.9-rc.1", prerelease=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.writes, [])


class MaintainersTests(unittest.TestCase):
    """A member whose organization membership is private reaches the workflow as CONTRIBUTOR or NONE. The maintainers
    file kept in the repository lets the workflow count that account as a member; nothing else widens trust. A line
    names the login and the account id, and both must match, so a login freed by a rename trusts nobody."""

    def _confirm(self, login, assoc, maintainers, account=77):
        """An outsider's detour, then a confirmation from `login` (account `account`) with `assoc`.
        -> (the detour's trust, stderr)."""
        fake = FakeGitHub()
        fake.registry_issue(12)
        fake.add_comment(12, _body(_payload("detour")), _user(2))
        cid = fake.add_comment(12, _body(_payload("confirm")), {"login": login, "id": account, "type": "User"}, assoc)
        rc, _, err = _run(fake, "issue_comment", 12, comment=cid, maintainers=maintainers)
        self.assertEqual(rc, 0, err)
        return fake.record(12)["detours"][0]["trust"], err

    def test_a_listed_login_confirms_a_detour_into_verified(self):
        listed = "# Maintainers whose membership is private\nalice-dev 77\n"
        self.assertEqual(self._confirm("alice-dev", "CONTRIBUTOR", listed)[0], "verified")
        self.assertEqual(self._confirm("Alice-Dev", "NONE", listed)[0], "verified")  # GitHub logins ignore case

    def test_an_unlisted_contributor_or_collaborator_does_not(self):
        for assoc in ("CONTRIBUTOR", "COLLABORATOR", "NONE"):
            self.assertEqual(self._confirm("bob", assoc, "alice-dev 77\n")[0], "proposed", assoc)

    def test_a_listed_login_on_another_account_does_not(self):
        # alice-dev renamed her account and someone registered the freed login; or the line names the wrong account
        self.assertEqual(self._confirm("alice-dev", "NONE", "alice-dev 77\n", account=78)[0], "proposed")
        self.assertEqual(self._confirm("alice", "NONE", "alice-dev 77\n")[0], "proposed")  # the renamed account

    def test_a_malformed_line_is_ignored_and_the_rest_still_count(self):
        text = "alice-dev bob\n@carol 77\nerin 77  # lead maintainer\n" + "x" * 40 + " 77\nfrank\ngil 0\n"
        for login in ("alice-dev", "bob", "carol", "x" * 40, "frank", "gil"):
            self.assertEqual(self._confirm(login, "CONTRIBUTOR", text)[0], "proposed", login)
        self.assertEqual(self._confirm("gil", "CONTRIBUTOR", text, account=0)[0], "proposed")
        trust, err = self._confirm("erin", "CONTRIBUTOR", text)
        self.assertEqual(trust, "verified")
        self.assertEqual(re.findall(r"line (\d+)", err), ["1", "2", "4", "5", "6"])

    def test_without_the_file_only_owner_and_member_count(self):
        unreadable = ROOT / ".github"  # a directory: read as no file, never as a reason to trust more
        for maintainers in (None, unreadable):
            got = {assoc: self._confirm("alice-dev", assoc, maintainers)[0]
                   for assoc in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR")}
            self.assertEqual(got, {"OWNER": "verified", "MEMBER": "verified", "COLLABORATOR": "proposed",
                                   "CONTRIBUTOR": "proposed"})

    def test_a_listed_maintainer_reopens_a_fixed_bug_alone(self):
        fake = _fixed(71)
        cid = fake.add_comment(71, _body(_payload("vote", version="26.11.9")), _user(5), "CONTRIBUTOR")
        self.assertEqual(_run(fake, "issue_comment", 71, comment=cid, maintainers="user5 5\n")[0], 0)
        self.assertEqual((fake.issues[71]["state"], fake.record(71)["status"]), ("open", "regressed"))

    def test_a_bug_a_listed_maintainer_closed_is_marked_fixed(self):
        for maintainers, fixed_in in ((None, ""), ("outsider 42\n", ""), ("outsider 41\n", "26.11.8")):
            fake = _released()
            rc, _, err = _run(fake, "release", maintainers=maintainers)
            self.assertEqual(rc, 0, err)
            self.assertEqual(fake.record(62)["fixed_in"], fixed_in)  # closed by hand, by a private member

    def test_the_workflow_reads_the_file_kept_in_the_repository(self):
        self.assertEqual(action.MAINTAINERS, ROOT / ".github" / "bug-registry-maintainers")
        err = io.StringIO()
        with redirect_stderr(err):
            listed = action.read_maintainers(action.MAINTAINERS)
        self.assertEqual(err.getvalue(), "")  # no ignored line
        self.assertTrue(listed)
        text = action.MAINTAINERS.read_text(encoding="utf-8")
        self.assertIn("pull request", text)
        self.assertNotIn("devkit", text.lower())  # no brand words: the rebrand rewrites one constant


def _command_report(**fields):
    """A command report with the reporter's own words; it hashes nothing, so its fingerprint is random."""
    return {**_payload("report", fingerprint="0123456789ab", exc_class="", frames=[],
                       title="dev progress forgets a step", text=REPORTED), "kind": "command", **fields}


def _edit_report(body, drop):
    """The issue body after its author edited the report in its data block by hand, taking out the fields `drop`."""
    m = re.search(r"```json\n(.*?)\n```", body, re.S)
    report = {k: v for k, v in json.loads(m.group(1)).items() if k not in drop}
    return body[:m.start()] + bugs.render_payload(report) + body[m.end():]


class IssueTextTests(unittest.TestCase):
    """The workflow writes each registry issue's title and body from its record, whenever their text differs."""

    def test_a_fold_writes_the_title_and_body_from_the_record(self):
        fake = FakeGitHub()
        fake.registry_issue(12)  # as an older workflow left it: a title someone typed, a body of data only
        vote = fake.add_comment(12, _body(_payload("vote", version="26.11.8")), _user(2))
        rc, _, err = _run(fake, "issue_comment", 12, comment=vote)
        self.assertEqual(rc, 0, err)
        issue = fake.issues[12]
        self.assertEqual((issue["title"], fake.record(12)["title"]), ("dev progress step crashes with KeyError",) * 2)
        self.assertIn("- **Impact:** Blocks work for everyone who hit it (2 people).", issue["body"])
        self.assertIn("- **Versions:** seen on 26.11.7 and 26.11.8.", issue["body"])
        self.assertEqual(action._first_report(fake.issue(12)), _payload("report"))  # its data reads as before
        self.assertFalse({_h(1), _h(2)} & set(re.findall(r"[0-9a-f]{64}", issue["body"])))

    def test_a_run_that_changes_no_text_writes_nothing(self):
        fake = FakeGitHub()
        fake.registry_issue(12)
        chat = fake.add_comment(12, "Same here.", _user(3))
        vote = fake.add_comment(12, _body(_payload("vote")), _user(2))
        self.assertEqual(_run(fake, "issue_comment", 12, comment=vote)[0], 0)
        self.assertIn(("edit_issue", 12), fake.writes)  # the text it wrote from the record
        fake.writes.clear()
        rc, _, err = _run(fake, "issue_comment", 12, comment=chat)  # an event that brings nothing new
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.writes, [])

    def test_a_body_edited_while_the_run_writes_is_read_again(self):
        fake = FakeGitHub()
        fake.add_issue(30, _body(_command_report()), _user(7))

        def reporter_takes_out_their_text(f):
            f.issues[30]["body"] = _edit_report(f.issues[30]["body"], ["text"])
        fake.after_record_write = reporter_takes_out_their_text
        rc, _, err = _run(fake, "issues", 30)
        self.assertEqual(rc, 0, err)
        body = fake.issues[30]["body"]
        self.assertNotIn(REPORTED, body)
        self.assertIn("The reporter's description: dev progress forgets a step", body)  # the words they kept
        self.assertEqual(fake.writes.count(("edit_issue", 30)), 1)

    def test_the_record_keeps_none_of_the_reporters_words_and_the_text_drops_what_they_take_out(self):
        fake = FakeGitHub()
        fake.add_issue(31, _body(_command_report()), _user(7))
        self.assertEqual(_run(fake, "issues", 31)[0], 0)
        self.assertIn(f"- The reporter's description: dev progress forgets a step — {REPORTED}",
                      fake.issues[31]["body"])  # shown while the reporter's own issue carries it
        self.assertNotIn(REPORTED, fake.record_comment(31)["body"])
        self.assertFalse({"title", "text", "template"} & set(fake.record(31)["first"]))

        fake.issues[31]["body"] = _edit_report(fake.issues[31]["body"], ["text"])
        vote = fake.add_comment(31, _body(_payload("vote", fp="0123456789ab")), _user(8))
        rc, _, err = _run(fake, "issue_comment", 31, comment=vote)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(REPORTED, fake.issues[31]["body"])
        self.assertNotIn(REPORTED, fake.record_comment(31)["body"])

    def test_an_older_record_gets_its_first_report_without_the_reporters_words(self):
        fake = FakeGitHub()
        fake.add_issue(32, _body(_command_report()), _user(7), labels=["devkit-bug", "kind:command"])
        rec = bugs.fold({}, _command_report(), author_hash=_h(7), author_assoc="NONE", now=NOW - DAY)
        older = {k: v for k, v in rec.items() if k not in ("first", "days_seen", "last_day")}
        older["voters"][_h(8)] = {"impact": "minor", "ts": NOW - 3 * DAY}
        cid = fake.add_comment(32, "", BOT, ts=NOW - DAY)
        fake.notes[cid]["body"] = bugs.render_record({**older, "title": "dev progress step misbehaves", "seen": cid})
        chat = fake.add_comment(32, "Same here.", _user(5))
        rc, _, err = _run(fake, "issue_comment", 32, comment=chat)
        self.assertEqual(rc, 0, err)
        rec = fake.record(32)
        self.assertEqual(rec["first"], {k: v for k, v in _command_report().items() if k not in ("title", "text")})
        self.assertEqual((rec["title"], fake.issues[32]["title"]), ("dev progress step does not work as expected",) * 2)
        self.assertNotIn(REPORTED, fake.record_comment(32)["body"])
        self.assertEqual((rec["days_seen"], rec["last_day"]), (2, "2027-01-14"))  # the days its voters last voted
        self.assertIn("- **How often:** 2 people in the last 30 days, on 2 days.", fake.issues[32]["body"])

    def test_a_duplicate_reads_as_the_first_issues_bug_and_points_to_it(self):
        fake = FakeGitHub()
        fake.registry_issue(7)
        fake.issues[7]["created_at"] = _iso(NOW - 5 * DAY)
        fake.add_issue(9, _body(_payload("report")), _user(2))
        rc, _, err = _run(fake, "issues", 9)
        self.assertEqual(rc, 0, err)
        dup = fake.issues[9]
        self.assertEqual(dup["title"], "dev progress step crashes with KeyError")
        self.assertIn("- **Status:** Duplicate of #7. Reports and votes for this bug count there.", dup["body"])
        self.assertIn("2 people in the last 30 days", dup["body"])  # the first issue's count, which it joined
        self.assertIn("First seen 2027-01-10", dup["body"])
        self.assertEqual(action._first_report(fake.issue(9)), _payload("report"))  # its own report stays readable

    def test_a_release_rewrites_the_text_of_the_bugs_it_marks_fixed(self):
        fake = _released()
        left = fake.issues[62]["body"]
        rc, _, err = _run(fake, "release")
        self.assertEqual(rc, 0, err)
        body = fake.issues[60]["body"]
        self.assertIn("- **Status:** Fixed in 26.11.8: run `dev update` to get it.", body)
        self.assertIn("- `dev update` gets the fix, in 26.11.8.", body)
        self.assertEqual(fake.issues[62]["body"], left)  # not fixed by this release: not rewritten by it


class _Response:
    def __init__(self, status=200, data=None, link=""):
        self.status, self._raw, self.headers = status, b"" if data is None else json.dumps(data).encode(), {
            "Link": link}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClientTests(unittest.TestCase):
    def _client(self, answers):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            answer = answers.pop(0)
            if isinstance(answer, int):
                raise urllib.error.HTTPError(req.full_url, answer, "error", {}, io.BytesIO(b'{"message":"no"}'))
            return answer
        return action.GitHub(repo="owner/registry", token="t0k3n-value", opener=opener), seen

    def test_requests_carry_the_token_in_a_header_and_read_every_page(self):
        client, seen = self._client([_Response(data=[{"id": 2}], link='<https://x?page=2>; rel="next"'),
                                     _Response(data=[{"id": 3}])])
        self.assertEqual([c["id"] for c in client.comments(12)], [2, 3])
        self.assertEqual([r.get_header("Authorization") for r in seen], ["Bearer t0k3n-value"] * 2)
        self.assertTrue(all(r.full_url.startswith("https://api.github.com/repos/owner/registry/issues/12/comments")
                            and "t0k3n" not in r.full_url for r in seen))

    def test_membership_closures_and_gone_comments(self):
        client, seen = self._client([_Response(status=204), 404, 404, _Response(data={"data": {"repository": {
            "issue": {"timelineItems": {"nodes": [{"actor": {"login": "maint", "databaseId": 9},
                                                   "stateReason": "COMPLETED",
                                                   "closer": {"__typename": "PullRequest", "merged": True}}]}}}}})])
        self.assertTrue(client.is_member("maint"))
        self.assertFalse(client.is_member("outsider"))
        self.assertTrue(client.is_member("Owner"))  # the repository's owner, without asking
        client.delete_comment(5)  # already gone: nothing to do
        self.assertEqual(client.closure(60), {"actor": "maint", "actor_id": 9, "merged_pr": True})
        self.assertEqual(len(seen), 4)
        with self.assertRaises(action.GitHubError):
            self._client([500])[0].issue(1)

    def test_the_duplicate_search_reads_only_registry_issues(self):
        label = [{"name": "devkit-bug"}]
        client, seen = self._client([_Response(data={"items": [
            {"number": 3, "labels": []}, {"number": 4, "labels": label},
            {"number": 5, "labels": label, "pull_request": {"url": "x"}}]})])
        self.assertEqual(client.find(FP), [4])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0].full_url).query)["q"][0]
        self.assertEqual(query, f"repo:owner/registry is:issue label:devkit-bug in:body {FP}")

    def test_the_client_needs_a_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(action.GitHubError):
                action.GitHub(repo="owner/registry")


class WorkflowFileTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_event_text_reaches_no_command(self):
        exprs = sorted(" ".join(e.split()) for e in re.findall(r"\$\{\{(.*?)\}\}", self.text, re.S))
        self.assertEqual(exprs, sorted([
            "!github.event.issue.pull_request && (github.event_name == 'release' || (github.event_name == 'issues' && "
            "contains(github.event.issue.body, '```json')) || (github.event_name == 'issue_comment' && "
            "contains(github.event.comment.body, '```json')))",  # the job's condition: compared, never run
            "github.event.issue.number || github.event.release.id",  # the concurrency group: numbers only
            "github.token", "secrets.BUG_REGISTRY_KEY"]))
        self.assertNotIn("github.event", self.text[self.text.index("\n    steps:"):])

    def test_only_the_allowed_permissions(self):
        lines = self.text.splitlines()
        starts = [i for i, ln in enumerate(lines) if re.match(r"\s*permissions:", ln)]
        self.assertEqual(len(starts), 2)
        self.assertEqual(lines[starts[0]], "permissions: {}")
        indent = len(lines[starts[1]]) - len(lines[starts[1]].lstrip())
        grants = []
        for ln in lines[starts[1] + 1:]:
            if not ln.strip() or len(ln) - len(ln.lstrip()) <= indent:
                break
            grants.append(ln.split("#")[0].strip())
        self.assertEqual(sorted(grants), ["contents: read", "issues: write"])
        self.assertNotRegex(self.text, r"write-all|read-all")

    def test_one_pinned_checkout_and_one_step_with_a_timeout(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        pin = re.compile(r"uses: actions/checkout@([0-9a-f]{40}) # v[\d.]+")
        self.assertEqual(pin.findall(self.text), pin.findall(ci)[:1])
        self.assertEqual(len(re.findall(r"\buses:", self.text)), 1)
        self.assertIn("persist-credentials: false", self.text)
        runs = [ln.strip() for ln in self.text.splitlines() if re.match(r"\s*(- )?run:", ln)]
        self.assertEqual(runs, ['run: python3 tools/bug_registry_action.py "$GITHUB_EVENT_PATH" "$GITHUB_EVENT_NAME"'])
        for line in ("runs-on: ubuntu-24.04", "timeout-minutes: 5", "cancel-in-progress: false",
                     "GITHUB_TOKEN: ${{ github.token }}", "BUG_REGISTRY_KEY: ${{ secrets.BUG_REGISTRY_KEY }}"):
            self.assertIn(line, self.text)
        self.assertNotRegex(self.text, r"pull_request_target|workflow_run|pip install")
        self.assertNotIn("devkit", self.text.lower())  # no brand words: the rebrand rewrites one constant

    def test_triggers(self):
        for event, action_ in (("issues", "opened"), ("issue_comment", "created"), ("release", "published")):
            self.assertRegex(self.text, rf"\n  {event}:( #[^\n]*)?\n    types: \[{action_}\]\n")
        ons = re.findall(r"^  ([a-z_]+):", self.text.split("\npermissions:")[0], re.M)
        self.assertEqual(ons, ["issues", "issue_comment", "release"])


if __name__ == "__main__":
    unittest.main()
