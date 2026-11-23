#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bug registry's workflow: keep one bot-written record per registry issue, and the issue's text written from it.

`.github/workflows/bug-registry.yml` runs this on `issues: opened` and `labeled`, `issue_comment: created`,
`release: published` and `workflow_dispatch`, with two arguments, the event file GitHub writes and the event name.
Event text is read from that file, never pasted into a command.

- An issue's first payload is its body. A valid report makes the issue a registry issue: it gets the labels and the
  record comment the bot writes. Anything else leaves the issue alone.
- Every write of a record also writes the issue's title and body from it (bugs.human_title, bugs.render_issue) when
  their text differs: after a fold, on a release that marks the bug fixed, and on a maintainer's `verdict:*` label,
  which the body states in one sentence. The record keeps the first report without the reporter's free text; the
  body shows that text only while the issue's own body still carries it, so a reporter who takes it out has it gone
  from the next render. It stays in the issue's public edit history: GitHub keeps every earlier body of an issue,
  the reporter's own and each one this workflow wrote, and no API removes them, so unlike a record comment an issue
  body cannot be posted anew. It stays public until each earlier body that holds it is deleted from the history on
  GitHub, or the issue is deleted. A record also forgets the accounts quiet for 90 days each time it is written (_pruned).
- A manual run (workflow_dispatch) rewrites every registry issue, open or closed in the last 90 days, without folding
  anything: one request a second at most, and at most RERENDER_MAX issues rewritten, so a second run goes on where
  the first stopped. It also creates the `verdict:*` labels maintainers apply.
- Each run folds every payload comment it has not folded yet, oldest first, so a run cancelled by the concurrency
  group loses nothing. The record keeps `seen`, the highest comment id handled, and `folds`, today's folded payloads
  per account, which cap every account at DAILY_CAP payloads a day across the registry. Folded votes, confirmations
  and failure marks are deleted; detours and reports stay on the issue; anything invalid is left alone. A payload
  whose check fails in any way is invalid, so no comment or issue a stranger writes stops a run, and a report counts
  only with the fingerprint its own fields give.
- The record always fits in one comment (bugs.render_record). If GitHub still refuses it, the record keeps its old
  content with this run's `seen` and `folds`: the payloads count toward the cap and are not folded again.
- GitHub shows every earlier body of an edited comment in its public edit history, and drops that history only with
  the comment, so a record comment is edited in place only while every account it ever named is still in the record
  and only this workflow edited it (_write). A write that drops an account, or that follows anyone else's edit (a
  maintainer who took an account out, an older workflow), posts the record as a new comment and then deletes the old
  one. Posting first means a run stopped between the two leaves two records, never none; the newest is the record,
  and the next write deletes the other.
- The manual run also posts anew the record of every registry issue closed longer ago that anyone but this workflow
  edited, or that still names accounts by the older digests: the same record, keyed per bug, as a new comment, and
  the old one goes with its history. A record posted anew has no edit, so a second run changes nothing.
- A later issue for a fingerprint that already has one is closed as its duplicate. It keeps a small record of its own
  (status `duplicate`), and its payloads, now and later, are folded into the first issue's record. The first issue is
  a registry issue (labeled devkit-bug, which only this workflow sets), never one that merely quotes a fingerprint.
- A published release sets `fixed_in` on the bugs closed as completed since the previous release by a merged pull
  request or by a member of the organization. A fixed bug that regresses (a member's vote, or three accounts', on a
  fixed version) is reopened and labeled.

Voters are HMAC-SHA256 digests keyed with the repository secret BUG_REGISTRY_KEY, never logins: a digest of the
GitHub account id that never leaves the run, keyed again with the bug's fingerprint for the record (_voter). One
account is one voter within a bug, and its digests on two bugs are unrelated, so the public records cannot link a
person's votes, detours and marks across issues. Each record publishes the day of a vote or mark, never its second.
A record from before (no `voter_hash`) is rekeyed the next time the workflow writes it (_rekeyed). Trust comes from
GitHub's `author_association`: OWNER and MEMBER count. A member whose membership is private reaches the workflow as
CONTRIBUTOR or NONE, so `.github/bug-registry-maintainers` lists such accounts by login and
account id, and an account matching both counts as MEMBER; a login freed by a rename trusts nobody. The file is read
from the workflow's own checkout (the default branch on issue events, the tagged commit on a release), so it changes
only through a merged pull request, never through an event or a fork.
Every rule about payloads, records, trust and score is lib/devkit/bugs.py's, the code every client runs, so the
workflow and the clients cannot disagree.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import bugs, core  # noqa: E402

DAILY_CAP = 20  # folded payloads per account per day, across the registry
DELETED = ("vote", "confirm", "failed")  # once folded these comments go; detours and reports stay on the issue
REGRESSION = "regression"
# A record this workflow writes says the comment's edit history names no account the record does not (_write): an
# older workflow's record lacks it, so its edited comment is posted anew once
EDIT_HISTORY = "same-accounts"
_DIGEST = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")  # an account digest, as _voter and _account write it
EXPIRED = "expired-"  # stands in for the account of a team mark older than VOTER_DAYS; a digest is 64 hex characters
EVENTS = {"issues": ("opened", "labeled"), "issue_comment": ("created",), "release": ("published",),
          "workflow_dispatch": (None,)}  # the actions each event runs for; a manual run's event has none
VERDICT = "verdict:"  # the labels maintainers give a bug, one for each of bugs.VERDICTS
VERDICT_COLOR = "c5def5"
RERENDER_MAX = 200  # issues one manual run rewrites; the next run goes on, skipping the ones already current
RERENDER_PACE_S = 1.0  # seconds between two requests of a manual run, inside GitHub's limits for the workflow token
TRIES = 3  # writes of one record that found it changed by another run, before the run gives up
WAITS, WAIT_S = 4, 15  # a duplicate waits up to a minute for the first issue's own run to write its record
ATTEMPTS, RETRY_S = 3, 2.0  # tries of one request GitHub may answer later, and the wait before the second
RETRIED = frozenset({0, 429, 500, 502, 503, 504})  # no answer (status 0), rate limited, or a server error
MAINTAINERS = Path(__file__).resolve().parents[1] / ".github" / "bug-registry-maintainers"
MAINTAINER_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})\s+([1-9][0-9]{0,15})")  # a login, then its account id
CLOSURE_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
        nodes { ... on ClosedEvent { actor { login ... on User { databaseId } }
                                       closer { __typename ... on PullRequest { merged } } } }
      }
    }
  }
}"""
EDITS_PAGE = 100  # edits one query reads; a comment edited more often reads as unknown, and is posted anew
EDITORS_QUERY = """query($id: ID!) {
  node(id: $id) {
    ... on IssueComment { userContentEdits(first: %d) { totalCount nodes { editor { __typename login } } } }
  }
}""" % EDITS_PAGE


class GitHubError(Exception):
    """A GitHub API call failed; `status` is the HTTP status GitHub answered with, 0 when it did not answer."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class RegistryError(Exception):
    """The run cannot finish now; the next event on the issue folds what is left."""


class Refused(RegistryError):
    """GitHub refused a folded record; the record kept its old content with this run's `seen` and `folds`."""


class Changed(Exception):
    """A record comment changed between this run reading it and writing it."""


# --------------------------------------------------------------------------- reading issues and comments

def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _midnight(now: float) -> float:
    """The start of `now`'s day in UTC: the time a record publishes for what happened during it."""
    return now - now % 86400


def _account(key: bytes, user) -> str:
    """The keyed digest of a GitHub account id, or '' for a user without an id. It stays inside the run: records
    carry _voter's digest, which differs from bug to bug. (Records an older workflow wrote carry this one.)"""
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    if isinstance(uid, bool) or not isinstance(uid, int):
        return ""
    return hmac.new(key, str(uid).encode(), hashlib.sha256).hexdigest()


def _per_bug(key: bytes, fp: str, account: str) -> str:
    """The digest that stands for an account in the record of the bug `fp`: the same for every payload of that bug,
    unrelated to the account's digest on any other bug without the key."""
    return hmac.new(key, b"bug\0" + fp.encode() + b"\0" + account.encode(), hashlib.sha256).hexdigest()


def _voter(key: bytes, user, fp: str) -> str:
    """The digest that stands for a GitHub account in the record of the bug `fp`, or '' for a user without an id."""
    account = _account(key, user)
    return _per_bug(key, fp, account) if account else ""


def _rekeyed(key: bytes, rec):
    """A record an older workflow wrote, with each account digest replaced by its digest for this bug: voters,
    regression marks, detour authors, confirmation and failure marks, and today's fold counts. Placeholders of
    expired team marks stay. A record already keyed per bug, a duplicate's stub, or None comes back as it is."""
    if not isinstance(rec, dict) or rec.get("voter_hash") == bugs.VOTER_HASH or rec.get("status") == "duplicate":
        return rec
    rec, fp = json.loads(json.dumps(rec)), str(rec.get("fingerprint") or "")

    def new(h):
        return _per_bug(key, fp, h) if isinstance(h, str) and h and not h.startswith(EXPIRED) else h
    if isinstance(rec.get("voters"), dict):
        rec["voters"] = {new(h): v for h, v in rec["voters"].items()}
    if isinstance(rec.get("regression"), list):
        rec["regression"] = [new(h) for h in rec["regression"]]
    for d in rec.get("detours") if isinstance(rec.get("detours"), list) else []:
        if not isinstance(d, dict):
            continue
        if "author_hash" in d:
            d["author_hash"] = new(d["author_hash"])
        for marks in (d.get("confirmations"), d.get("failures")):
            for m in marks if isinstance(marks, list) else []:
                if isinstance(m, dict) and "author_hash" in m:
                    m["author_hash"] = new(m["author_hash"])
    folds = rec.get("folds")
    if isinstance(folds, dict) and isinstance(folds.get("by"), dict):
        folds["by"] = {new(h): n for h, n in folds["by"].items()}
    rec["voter_hash"] = bugs.VOTER_HASH
    return rec


def read_maintainers(path: Path = MAINTAINERS) -> frozenset:
    """The (login in lower case, account id) pairs the maintainers file lists; GitHub logins ignore case. One account
    per line, its login then its numeric id, and `#` starts a comment. Any other line, a login without its id among
    them, is ignored with a warning. A missing or unreadable file lists nobody, so only OWNER and MEMBER count."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError) as e:
        print(f"bug registry: cannot read {Path(path).name}, so it lists nobody: {e}", file=sys.stderr)
        return frozenset()
    accounts = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = MAINTAINER_RE.fullmatch(line)
        if m:
            accounts.add((m.group(1).lower(), int(m.group(2))))
        else:
            print(f"bug registry: {Path(path).name} line {n} is not a GitHub login and its account id: ignored",
                  file=sys.stderr)
    return frozenset(accounts)


def _listed(login, account, maintainers: frozenset) -> bool:
    """Whether the maintainers file lists this account under this login."""
    return isinstance(login, str) and _number(account) > 0 and (login.lower(), account) in maintainers


def _association(user, assoc, maintainers: frozenset) -> str:
    """The author_association bugs.py's trust rule sees: GitHub's, or MEMBER for a listed maintainer that GitHub shows
    as anything less. Mapping here keeps one trust rule for the workflow and the clients."""
    assoc = assoc if isinstance(assoc, str) else ""
    if assoc in bugs.TRUSTED or not isinstance(user, dict):
        return assoc
    return "MEMBER" if _listed(user.get("login"), user.get("id"), maintainers) else assoc


def _by_bot(comment: dict) -> bool:
    user = comment.get("user") or {}
    return user.get("login") == bugs.BOT_LOGIN and user.get("type") == "Bot"


def _payload(body):
    """The valid payload a body holds, or None. A report must carry the fingerprint its own fields give
    (bugs.fingerprint_matches), so it cannot give another bug its title. An error while checking one payload makes
    that payload invalid: whatever one comment or issue holds, the fold of the others goes on."""
    try:
        p = bugs.parse_payload(body)
        if p is None or bugs.validate_payload(p):
            return None
        return p if p["type"] != "report" or bugs.fingerprint_matches(p) else None
    except Exception as e:  # noqa: BLE001 - a stranger's payload must never stop the run
        print(f"bug registry: a payload could not be checked ({type(e).__name__}), so it is left alone",
              file=sys.stderr)
        return None


def _first_report(issue: dict):
    """The report an issue's body holds, when it is a valid one: the issue's first payload."""
    p = _payload(issue.get("body"))
    return p if p is not None and p["type"] == "report" else None


def _labels(issue: dict) -> set:
    return {lb.get("name") if isinstance(lb, dict) else lb for lb in issue.get("labels") or []}


def _number(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read(client, number: int):
    """-> (the issue's comments in id order, the record comment, its record or None). The record comment is the
    bot's newest comment with the record marker, readable or not, so a broken one is rewritten rather than doubled.
    A write that posts the record anew does so before it deletes the old comment (_write), so a run stopped between
    the two leaves an older record comment behind: the holder lists those under "older", and the next write deletes
    them."""
    comments = sorted((c for c in client.comments(number) or [] if isinstance(c, dict) and _number(c.get("id"))),
                      key=lambda c: c["id"])
    held = [c for c in comments if _by_bot(c) and bugs.RECORD_MARK in (c.get("body") or "")]
    if not held:
        return comments, None, None
    holder = {**held[-1], "older": [c["id"] for c in held[:-1]]}
    return comments, holder, bugs.parse_record(holder.get("body"))


def _edited(comment: dict) -> bool:
    """Whether GitHub keeps an edit history for the comment: its update time is not its creation time."""
    created, updated = comment.get("created_at"), comment.get("updated_at")
    return bool(created and updated and created != updated)


def _accounts(body) -> set:
    """The account digests a record comment names, wherever the record keeps them (voters, regression marks, detour
    authors and marks, fold counts)."""
    return set(_DIGEST.findall(body or ""))


def _workflow_account(account: dict) -> bool:
    """The account is this workflow's bot: REST names it github-actions[bot], GraphQL github-actions."""
    login = str(account.get("login") or "")
    return account.get("type") == "Bot" and login in (bugs.BOT_LOGIN, bugs.BOT_LOGIN[:-len("[bot]")])


def _edited_by_others(client, holder: dict) -> bool:
    """Whether the record comment's public edit history may keep a body this workflow did not write as its current
    rules do, so a person may have taken out an account the workflow's earlier bodies still name there: an older
    workflow edited it (its record lacks EDIT_HISTORY), its text is not what the workflow renders for its own record
    (a hand edit, told without asking GitHub), or an account other than the workflow's edited it
    (client.comment_editors). What cannot be read counts as edited by others. A comment never edited has no
    history: False."""
    if not _edited(holder):
        return False
    body = holder.get("body")
    old = bugs.parse_record(body)
    if old is None or old.get("edit_history") != EDIT_HISTORY or body != bugs.render_record(old):
        return True
    try:
        editors = client.comment_editors(holder.get("node_id"))
    except GitHubError as e:
        print(f"bug registry: the edit history of comment {holder.get('id')} could not be read ({e}), so the "
              f"record is posted anew", file=sys.stderr)
        return True
    return editors is None or not all(isinstance(e, dict) and _workflow_account(e) for e in editors)


def _write(client, number: int, holder, rec: dict, others=None) -> bool:
    """Write the record when the comment's text differs from it (after a fold, or after a person edited the
    comment), when someone other than this workflow edited the comment, or when an older record comment is left.
    GitHub keeps every earlier body of an edited comment in a public edit history, which goes only with the comment,
    and emails the issue's subscribers for a new comment but not for an edit. So the record is edited in place while
    every account its comment ever named is still in it, and posted anew when an account leaves it, or when anyone
    but this workflow edited the comment (_edited_by_others; `others` when the caller already asked): a person who
    takes an account out leaves it in the workflow's earlier bodies. The new comment comes first and the old one is
    deleted after, so a run stopped between the two loses no record (_read takes the newest). The comment is read
    again just before; if it changed or went since this run read it, Changed says to start over. -> whether it
    wrote."""
    body = bugs.render_record({**rec, "edit_history": EDIT_HISTORY})
    if holder is None:
        client.create_comment(number, body)
        return True
    gone, wrote = [cid for cid in holder.get("older") or [] if _number(cid)], False
    anew = bool(_accounts(holder.get("body")) - _accounts(body)) or \
        (_edited_by_others(client, holder) if others is None else others)
    if holder.get("body") != body or anew:
        if (client.comment(holder["id"]) or {}).get("body") != holder.get("body"):
            raise Changed(number)
        if anew:
            client.create_comment(number, body)
            gone.append(holder["id"])
        else:
            client.edit_comment(holder["id"], body)
        wrote = True
    for cid in gone:
        client.delete_comment(cid)
    return wrote or bool(gone)


# --------------------------------------------------------------------------- what the record keeps, and the issue's text

def _pruned(rec: dict, now: float) -> dict:
    """The record without what it keeps about accounts quiet for bugs.VOTER_DAYS: their voter entries and totals, the
    regression marks of voters it no longer names (a mark has no time of its own), a daily fold count of a day that
    long ago, and the detour marks of accounts outside the team. A team member's older mark keeps its place under a
    placeholder instead of the account's digest: clients count a detour's marks to decide its trust, so a confirmed
    workaround stays served and a retired one stays retired."""
    rec = json.loads(json.dumps(rec))
    cut = now - bugs.VOTER_DAYS * 86400

    def recent(entry) -> bool:
        ts = entry.get("ts") if isinstance(entry, dict) else None
        return isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= cut
    if isinstance(rec.get("voters"), dict):
        rec["voters"] = {h: v for h, v in rec["voters"].items() if recent(v)}
    if isinstance(rec.get("older_voters"), dict):
        rec["older_voters"] = {k: t for k, t in rec["older_voters"].items() if recent(t)}
        if not rec["older_voters"]:
            del rec["older_voters"]
    if isinstance(rec.get("regression"), list):
        rec["regression"] = [h for h in rec["regression"] if h in (rec.get("voters") or {})]
    folds = rec.get("folds")
    if isinstance(folds, dict) and not (isinstance(folds.get("day"), str) and folds["day"] >= _today(cut)):
        del rec["folds"]
    for d in rec.get("detours") if isinstance(rec.get("detours"), list) else []:
        for key in ("confirmations", "failures"):
            marks = d.get(key) if isinstance(d, dict) else None
            if not isinstance(marks, list):
                continue
            used, kept = {m.get("author_hash") for m in marks if isinstance(m, dict)}, []
            for m in marks:
                if not isinstance(m, dict) or recent(m) or str(m.get("author_hash") or "").startswith(EXPIRED):
                    kept.append(m)
                elif m.get("author_assoc") in bugs.TRUSTED:
                    n = next(n for n in range(1, len(used) + 2) if f"{EXPIRED}{n}" not in used)
                    used.add(f"{EXPIRED}{n}")
                    kept.append({**m, "author_hash": f"{EXPIRED}{n}"})
            d[key] = kept
    return rec


def _first_reports(holder: dict, issue: dict) -> tuple:
    """(the first report a record keeps, the first report the issue's text shows). The record keeps only the report's
    closed-list fields, never the reporter's free text (bugs.FREE_FIELDS), so nothing the reporter typed lands in a
    comment the bot writes; a record from before it kept one takes it from the issue's body. The text shows the free
    text only while the issue's own body still carries it in a valid report of this bug: once the reporter takes it
    out, the next render leaves it out (the issue's edit history keeps the earlier bodies). `holder` is the record
    whose report counts: a duplicate's own stub."""
    free = bugs.FREE_FIELDS
    body = _first_report(issue)
    if body is not None and body.get("fingerprint") != holder.get("fingerprint"):
        body = None
    kept = holder.get("first") if isinstance(holder.get("first"), dict) else body or {}
    kept = {k: v for k, v in kept.items() if k not in free}
    return kept, {**kept, **{k: body[k] for k in free if body and k in body}}


def _verdict(labels: set, current, added=None):
    """The verdict the issue's labels give: the `verdict:*` label just added, else the record's verdict while its
    label stays on, else the first label left in the closed list's order; None without one. A label outside the
    closed list (bugs.VERDICTS) is ignored."""
    have = [v for v in bugs.VERDICTS if VERDICT + v in labels]
    return next((v for v in (added, current) if v in have), have[0] if have else None)


def _prepared(rec: dict, issue: dict, now: float, added=None) -> dict:
    """The record as this run writes it: pruned, with the first report it keeps, the human title, and the verdict
    of the issue's labels (`added`: the verdict a label event just added). A record from before the days were counted
    starts at the days its voters last voted, which is never more than the days the bug was seen."""
    rec = _pruned(rec, now)
    kept, _ = _first_reports(rec, issue)
    if kept:
        rec["first"] = kept
    if not isinstance(rec.get("days_seen"), int) or isinstance(rec.get("days_seen"), bool):
        days = sorted({_today(v["ts"]) for v in (rec.get("voters") or {}).values() if isinstance(v, dict)
                       and isinstance(v.get("ts"), (int, float)) and not isinstance(v.get("ts"), bool)})
        if days:
            rec["days_seen"], rec["last_day"] = len(days), days[-1]
    rec["title"] = bugs.human_title(rec)
    triage = rec.get("triage") if isinstance(rec.get("triage"), dict) else {}
    verdict = _verdict(_labels(issue), triage.get("verdict"), added)
    if verdict:
        rec["triage"] = {**triage, "verdict": verdict}
    else:
        rec.pop("triage", None)
    return rec


def _write_text(client, number: int, rec: dict, issue: dict, now: float, holder: dict = None,
                created: str = None) -> bool:
    """Write the issue's title and body from the record when their text differs. Before writing a body the issue is
    read again; when its body changed since this run read it (its author took out their own text, say), the text is
    written from the body as it is now. `holder` keeps the first report (a duplicate's stub), `created` the day the
    bug was first seen (the first issue's for a duplicate). -> whether it wrote."""
    for _ in range(TRIES):
        _, shown = _first_reports(holder or rec, issue)
        day = created or str(issue.get("created_at") or "")[:10]
        text = {"title": bugs.human_title(rec), "body": bugs.render_issue(rec, shown, created=day, now=now)}
        fields = {k: v for k, v in text.items() if issue.get(k) != v}
        if not fields:
            return False
        if "body" in fields:
            fresh = client.issue(number) or {}
            if fresh.get("body") != issue.get("body"):
                issue = fresh
                continue
        client.edit_issue(number, **fields)
        return True
    raise RegistryError(f"the body of issue #{number} kept changing while this run wrote it")


def _write_folded(client, number: int, holder, before, after: dict):
    """Write a folded record. When GitHub refuses it (HTTP 422, a comment it will not take), write the record as it
    was with this run's `seen` and `folds` instead, so the payloads still count toward their accounts' daily cap and
    no later run folds them again, and raise Refused so the run shows it. bugs.render_record keeps every record under
    GitHub's limit; this is the check behind it."""
    try:
        _write(client, number, holder, after)
    except GitHubError as e:
        if e.status != 422 or before is None:
            raise
        _write(client, number, holder, {**before, **{k: after[k] for k in ("seen", "folds") if k in after}})
        raise Refused(f"GitHub refused the record of issue #{number} ({e}): this run's payloads are counted and left "
                      f"unfolded") from None


# --------------------------------------------------------------------------- folding

def _entries(key: bytes, issue: dict, comments: list, rec, fp: str, maintainers: frozenset) -> list:
    """What this run folds, oldest first: the issue's body when it has no record yet, then every comment past `seen`
    holding a valid payload for this fingerprint. -> [(payload, voter, author_association, comment id or None,
    account)], with the voter's digest for this bug, the account's digest for the daily cap (never written), and a
    listed maintainer's association as MEMBER."""
    out = []
    if rec is None:
        out.append((_first_report(issue), issue.get("user"),
                    _association(issue.get("user"), issue.get("author_association"), maintainers), None))
    seen = _number((rec or {}).get("seen"))
    for c in comments:
        if c["id"] <= seen or _by_bot(c):
            continue
        p = _payload(c.get("body"))
        if p is not None and p["fingerprint"] == fp:
            out.append((p, c.get("user"), _association(c.get("user"), c.get("author_association"), maintainers),
                        c["id"]))
    return [(p, _voter(key, user, fp), assoc, cid, _account(key, user)) for p, user, assoc, cid in out
            if p is not None and _account(key, user)]


def _used_today(client, key: bytes, now: float, accounts) -> dict:
    """{account: payloads folded today} for these account digests, over every record the bot changed today. Each
    record counts an account under its digest for that record's bug, or under the account digest itself when an
    older workflow wrote the record, so the cap spans the registry while no record names an account twice."""
    day, used = _today(now), {a: 0 for a in accounts}
    for c in client.updated_comments(f"{day}T00:00:00Z") or []:
        if not isinstance(c, dict) or not _by_bot(c):
            continue
        rec = bugs.parse_record(c.get("body")) or {}
        folds = rec.get("folds")
        if not (isinstance(folds, dict) and folds.get("day") == day and isinstance(folds.get("by"), dict)):
            continue
        per_bug = rec.get("voter_hash") == bugs.VOTER_HASH
        for a in used:
            used[a] += _number(folds["by"].get(_per_bug(key, rec["fingerprint"], a) if per_bug else a))
    return used


def _fold_all(rec, entries: list, used: dict, now: float):
    """Fold the entries into `rec` in order, skipping the payloads of an account that reached DAILY_CAP today.
    Every time the record keeps is the day's start (_midnight). -> (the record, the entries folded); `used`, by
    account, and the record's `folds`, by voter, count them."""
    day = _today(now)
    folds = (rec or {}).get("folds")
    by = dict(folds["by"]) if isinstance(folds, dict) and folds.get("day") == day and \
        isinstance(folds.get("by"), dict) else {}
    done = []
    for entry in entries:
        payload, who, assoc, _, account = entry
        if used.get(account, 0) >= DAILY_CAP:
            continue
        new = bugs.fold(rec, payload, author_hash=who, author_assoc=assoc, now=_midnight(now))
        if not new:
            continue  # a record starts only with a report
        rec = new
        used[account] = used.get(account, 0) + 1
        by[who] = by.get(who, 0) + 1
        done.append(entry)
    if rec and by:
        rec["folds"] = {"day": day, "by": by}
    return rec, done


def _regressed(client, number: int, before, after: dict, issue: dict = None):
    """A fixed bug that regressed under fold's rule (a member's vote, or three accounts') is reopened and labeled."""
    if after.get("status") != "regressed" or (before or {}).get("status") == "regressed":
        return
    client.add_labels(number, [REGRESSION])
    if (issue or client.issue(number)).get("state") == "closed":
        client.edit_issue(number, state="open", state_reason="reopened")


def _delete(client, done: list):
    for payload, _, _, cid, _ in done:
        if cid is not None and payload["type"] in DELETED:
            client.delete_comment(cid)


def _first_issue(client, fp: str, number: int, sleep):
    """The lowest-numbered other registry issue for `fp` below `number`, or None. When the only candidates are issues
    whose own first run has not written a record yet, it waits for them; after WAITS waits, RegistryError."""
    for attempt in range(WAITS + 1):
        found = {n for n in client.find(fp) or [] if _number(n)}
        found |= {_number(i.get("number")) for i in client.recent_issues() or [] if isinstance(i, dict)
                  and not i.get("pull_request") and (_first_report(i) or {}).get("fingerprint") == fp}
        pending = []
        for n in sorted(n for n in found if 0 < n < number):
            _, _, rec = _read(client, n)
            if rec is not None:
                if rec.get("fingerprint") == fp and rec.get("status") != "duplicate":
                    return n
            elif (_first_report(client.issue(n)) or {}).get("fingerprint") == fp:
                pending.append(n)
        if not pending:
            return None
        if attempt < WAITS:
            sleep(WAIT_S)
    raise RegistryError(f"issue #{number} repeats the bug of issue #{pending[0]}, which has no record yet; "
                        f"the next event on #{number} folds it")


def _fold_into(client, key: bytes, target: int, entries: list, now: float) -> tuple:
    """Fold a duplicate's payloads into the first issue's record. -> (the entries folded, the record written). The
    first issue's text follows at its own next event."""
    for _ in range(TRIES):
        _, holder, rec = _read(client, target)
        if rec is None or rec.get("status") == "duplicate":
            raise RegistryError(f"issue #{target} has no record to fold a duplicate into")
        rec = _rekeyed(key, rec)
        new, done = _fold_all(rec, entries, _used_today(client, key, now, {e[4] for e in entries}), now)
        try:
            _write_folded(client, target, holder, rec, new)
        except Changed:
            continue
        _regressed(client, target, rec, new)
        return done, new
    raise RegistryError(f"the record of issue #{target} kept changing while this run wrote it")


def _forward(client, key: bytes, number: int, issue: dict, holder, stub: dict, entries: list, seen: int,
             now: float) -> str:
    """A duplicate's payloads go to the first issue; the duplicate keeps its stub record, so nothing is folded twice,
    has no labels, and is closed once with a pointer to the first issue. Its text reads as the first issue's bug,
    with "Duplicate of #n" for its status, and keeps its own first report."""
    target = _number(stub.get("duplicate_of"))
    if not target or target == number:
        raise RegistryError(f"the duplicate record of issue #{number} names no other issue")
    kept, _ = _first_reports(stub, issue)
    stub = {**stub, "seen": seen, **({"first": kept} if kept else {})}
    try:
        done, rec = _fold_into(client, key, target, entries, now) if entries else ([], None)
    except Refused:
        _write(client, number, holder, stub)  # counted on the first issue: not moved again
        raise
    _write(client, number, holder, stub)
    _delete(client, done)
    if issue.get("state") != "closed":
        client.edit_issue(number, state="closed", state_reason="not_planned")
        client.create_comment(number, f"Duplicate of #{target}. Reports and votes for this bug count there.")
    rec = rec if rec is not None else _read(client, target)[2]
    if rec is not None and rec.get("status") != "duplicate":
        first = str((client.issue(target) or {}).get("created_at") or "")[:10]
        _write_text(client, number, {**rec, "status": "duplicate", "duplicate_of": target}, issue, now, stub, first)
    return f"#{number} duplicates #{target}: {len(done)} payload(s) moved"


def _fold_issue_once(client, key: bytes, number: int, now: float, sleep, maintainers: frozenset,
                     added=None) -> str:
    issue = client.issue(number) or {}
    if issue.get("pull_request"):
        return f"#{number} is a pull request: skipped"
    comments, holder, rec = _read(client, number)
    rec = _rekeyed(key, rec)
    first = _first_report(issue) if rec is None else None
    if rec is None and first is None:
        return f"#{number} has no valid first payload: left alone"
    fp = (rec or first)["fingerprint"]
    entries = _entries(key, issue, comments, rec, fp, maintainers)
    # the bot's own comments are never payloads, and its record comment is posted anew on each write: counting it
    # would make every later run see a new `seen` and post the record again
    seen = max([c["id"] for c in comments if not _by_bot(c)] + [_number((rec or {}).get("seen"))])
    if rec is None:
        target = _first_issue(client, fp, number, sleep)
        if target is not None:
            rec = {"devkit_bug": bugs.SCHEMA_VERSION, "fingerprint": fp, "status": "duplicate", "duplicate_of": target}
    if rec is not None and rec.get("status") == "duplicate":
        return _forward(client, key, number, issue, holder, rec, entries, seen, now)
    new, done = _fold_all(rec, entries, _used_today(client, key, now, {e[4] for e in entries}) if entries else {},
                          now)
    if not new:
        return f"#{number}: its first payload is past its author's daily cap; the next event folds it"
    new["seen"] = seen
    new = _prepared(new, issue, now, added)
    _write_folded(client, number, holder, rec, new)
    missing = [lb for lb in (bugs.LABEL, f"kind:{new['kind']}") if lb not in _labels(issue)]
    if missing:
        client.add_labels(number, missing)
    _regressed(client, number, rec, new, issue)
    _delete(client, done)
    _write_text(client, number, new, issue, now)
    return f"#{number}: {len(done)} payload(s) folded"


def _fold_issue(client, key: bytes, number: int, now: float, sleep, maintainers: frozenset, added=None) -> str:
    """Fold what the issue has not folded yet and write its record and text; `added` is the verdict a label event
    just added."""
    for _ in range(TRIES):
        try:
            return _fold_issue_once(client, key, number, now, sleep, maintainers, added)
        except Changed:
            continue
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


# --------------------------------------------------------------------------- releases

def _mark_fixed(client, key: bytes, number: int, version: str, issue: dict, now: float) -> bool:
    """Set the version that fixes the bug, and write the issue's text with it, so "Fixed in" shows the day the
    release is published."""
    for _ in range(TRIES):
        _, holder, rec = _read(client, number)
        if rec is None or rec.get("status") == "duplicate":
            return False
        rec = _rekeyed(key, rec)
        old = core.version_key(rec.get("fixed_in"))
        if old and old >= core.version_key(version) and rec.get("status") != "regressed":
            return False
        new = _prepared({**rec, "fixed_in": version, "status": "fixed", "regression": []}, issue, now)
        try:
            _write(client, number, holder, new)
        except Changed:
            continue
        _write_text(client, number, new, issue, now)
        return True
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


def _release(client, key: bytes, event: dict, maintainers: frozenset, now: float) -> str:
    """Set `fixed_in` on the bugs closed as completed since the previous release by a merged pull request, a member
    or a listed maintainer. A draft or a prerelease marks nothing."""
    rel = event.get("release") if isinstance(event.get("release"), dict) else {}
    if rel.get("draft") or rel.get("prerelease"):
        return "a draft or a prerelease: no bug marked fixed"
    tag = str(rel.get("tag_name") or "")
    version = tag[1:] if tag.startswith("v") else tag
    vkey = core.version_key(version)
    if not vkey or ".".join(map(str, vkey)) != version:
        return "the tag is not a release version: no bug marked fixed"
    published = str(rel.get("published_at") or "")
    since = max((str(r["published_at"]) for r in client.releases() or [] if isinstance(r, dict)
                 and r.get("id") != rel.get("id") and not r.get("draft") and not r.get("prerelease")
                 and r.get("published_at") and (not published or str(r["published_at"]) < published)), default="")
    marked = []
    for issue in client.closed_issues(bugs.LABEL, since) or []:
        number, closed = _number(issue.get("number")), str(issue.get("closed_at") or "")
        if not number or issue.get("pull_request") or issue.get("state_reason") != "completed" or \
                (since and closed < since) or (published and closed > published):
            continue
        how = client.closure(number) or {}
        actor = str(how.get("actor") or "")
        if not how.get("merged_pr") and not (actor and (_listed(actor, how.get("actor_id"), maintainers)
                                                        or client.is_member(actor))):
            continue
        if _mark_fixed(client, key, number, version, issue, now):
            marked.append(number)
    return f"release {version}: {len(marked)} bug(s) marked fixed" + \
        (" (" + ", ".join(f"#{n}" for n in marked) + ")" if marked else "")


# --------------------------------------------------------------------------- the manual run

def _rerender(client, key: bytes, issue: dict, now: float):
    """Write one registry issue's record and text again without folding anything: `seen`, the fold counts of today
    and the score stay as they are, and a record an older workflow wrote is rekeyed per bug. -> whether it wrote, or
    None for an issue with no record of its own yet (its next event folds it)."""
    number = _number(issue.get("number"))
    for _ in range(TRIES):
        _, holder, rec = _read(client, number)
        if rec is None or rec.get("status") == "duplicate":
            return None
        new = _prepared(_rekeyed(key, rec), issue, now)
        try:
            wrote = _write(client, number, holder, new)
        except Changed:
            continue
        return _write_text(client, number, new, issue, now) or wrote
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


def _repost(client, key: bytes, issue: dict):
    """Post the record of a registry issue closed more than bugs.CLOSED_DAYS ago anew when anyone but this workflow
    edited its comment (_edited_by_others: an older workflow or a person) or it names accounts by digests from before
    they were keyed per bug: the same record, its accounts rekeyed per bug and nothing pruned, so its votes and counts
    stay; the issue's text is left as it is. A record that needs neither is left as it is. -> whether it wrote, or
    None for an issue without a readable record of its own."""
    number = _number(issue.get("number"))
    for _ in range(TRIES):
        _, holder, rec = _read(client, number)
        if rec is None or rec.get("status") == "duplicate":
            return None
        new = _rekeyed(key, rec)
        others = _edited_by_others(client, holder)
        if new == rec and not others and not holder.get("older"):
            return False
        try:
            return _write(client, number, holder, new, others)
        except Changed:
            continue
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


def _verdict_labels(client) -> list:
    """Create the `verdict:*` labels the repository lacks, each described by the sentence the issue's text states.
    -> the labels created."""
    have = {lb.get("name") for lb in client.labels() or [] if isinstance(lb, dict)}
    made = [VERDICT + v for v in bugs.VERDICTS if VERDICT + v not in have]
    for name in made:
        client.create_label(name, VERDICT_COLOR, bugs.VERDICTS[name[len(VERDICT):]][:100])
    return made


def _rerender_all(client, key: bytes, now: float) -> str:
    """Rewrite every registry issue, open or closed in the last 90 days, and post anew the record an older workflow
    edited or keyed of every one closed longer ago (_repost), oldest first, at one request a second at most. It stops once it
    has written RERENDER_MAX issues and says how many are left; issues already current cost one read each, so
    running it again goes on where it stopped."""
    client.pace(RERENDER_PACE_S)
    made = _verdict_labels(client)
    cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - bugs.CLOSED_DAYS * 86400))
    issues = sorted((i for i in client.registry_issues("") or [] if isinstance(i, dict) and _number(i.get("number"))
                     and not i.get("pull_request") and bugs.LABEL in _labels(i)), key=lambda i: i["number"])
    issues = list({i["number"]: i for i in issues}.values())
    counts = {"rewritten": 0, "reposted": 0, "current": 0, "no record": 0}
    for n, issue in enumerate(issues):
        if counts["rewritten"] + counts["reposted"] >= RERENDER_MAX:
            break
        recent = issue.get("state") != "closed" or str(issue.get("closed_at") or "") >= cut
        try:
            wrote = _rerender(client, key, issue, now) if recent else _repost(client, key, issue)
        except (GitHubError, RegistryError) as e:
            raise RegistryError(f"stopped at #{issue['number']} ({e}): {_tally(counts, made)}, "
                                f"{len(issues) - n} left") from None
        counts[("rewritten" if recent else "reposted") if wrote else "current" if wrote is False else "no record"] += 1
    return f"{_tally(counts, made)}, {len(issues) - sum(counts.values())} left"


def _tally(counts: dict, made: list) -> str:
    return (f"created {len(made)} verdict label(s); " if made else "") + \
        f"rewrote {counts['rewritten']} issue(s), posted anew {counts['reposted']} record(s) of bugs closed more " \
        f"than {bugs.CLOSED_DAYS} days ago, {counts['current']} already current, " \
        f"{counts['no record']} without a record of their own"


# --------------------------------------------------------------------------- entry point

def _fail(message: str, code: int) -> int:
    print(f"bug registry: {message}", file=sys.stderr)
    return code


def main(event_path: str, event_name: str, client, *, now: float = None, sleep=time.sleep,
         maintainers: Path = MAINTAINERS) -> int:
    """Handle one event. -> 0 when done or when there is nothing to do, 1 when the run could not finish (the next
    event on the issue folds what is left), 2 when the registry secret is missing. `maintainers` is the maintainers
    file to read."""
    try:
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError) as e:
        return _fail(f"cannot read the event file: {e}", 1)
    if not isinstance(event, dict):
        return _fail("the event file holds no event", 1)
    issue = event.get("issue") if isinstance(event.get("issue"), dict) else {}
    if issue.get("pull_request"):
        print("bug registry: a pull request: skipped")
        return 0
    if event.get("action") not in EVENTS.get(event_name, ()):
        print("bug registry: not an event the registry handles: skipped")
        return 0
    added = None
    if event.get("action") == "labeled":
        name = (event.get("label") or {}).get("name") if isinstance(event.get("label"), dict) else None
        added = name[len(VERDICT):] if isinstance(name, str) and name.startswith(VERDICT) else None
        if added not in bugs.VERDICTS:
            print("bug registry: not a verdict label: skipped")
            return 0
    if event_name in ("issues", "issue_comment") and bugs.LABEL not in _labels(issue) and _first_report(issue) is None:
        print("bug registry: not a registry issue: skipped")
        return 0
    key = (os.environ.get("BUG_REGISTRY_KEY") or "").strip()
    if not key:
        return _fail("the repository secret BUG_REGISTRY_KEY is not set, so nothing was folded. A maintainer creates "
                     "it once (32 random bytes as hex) under Settings, Secrets and variables, Actions.", 2)
    now = time.time() if now is None else now
    listed = read_maintainers(maintainers)
    try:
        if event_name == "release":
            message = _release(client, key.encode(), event, listed, now)
        elif event_name == "workflow_dispatch":
            message = _rerender_all(client, key.encode(), now)
        else:
            number = _number(issue.get("number"))
            if not number:
                return _fail("the event names no issue", 1)
            message = _fold_issue(client, key.encode(), number, now, sleep, listed, added)
    except (GitHubError, RegistryError) as e:
        return _fail(str(e), 1)
    print(f"bug registry: {message}")
    return 0


# --------------------------------------------------------------------------- GitHub

def _message(err: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(err.read().decode("utf-8")).get("message") or "")[:200]
    except (ValueError, AttributeError, OSError):
        return ""


class GitHub:
    """The registry repository through GitHub's REST and GraphQL APIs, with the workflow's token from GITHUB_TOKEN.
    `opener` replaces urllib's in tests."""

    API = "https://api.github.com"

    def __init__(self, repo: str = None, token: str = None, timeout: float = 30, opener=None, sleep=time.sleep,
                 clock=time.monotonic):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise GitHubError("GITHUB_REPOSITORY must name the repository as owner/name")
        if not self.token:
            raise GitHubError("GITHUB_TOKEN is not set")
        self.timeout, self._open = timeout, opener or urllib.request.urlopen
        self._sleep, self._clock, self._gap, self._last = sleep, clock, 0.0, None

    def pace(self, seconds: float) -> None:
        """Start every later request at least `seconds` after the one before it, pages of a listing included."""
        self._gap = max(0.0, float(seconds))

    def _wait(self) -> None:
        if self._gap and self._last is not None and self._clock() - self._last < self._gap:
            self._sleep(self._gap - (self._clock() - self._last))
        self._last = self._clock()

    def _call(self, method: str, path: str, params: dict = None, body=None, missing_ok: bool = False,
              retry: bool = None):
        """One request, tried up to ATTEMPTS times when GitHub may answer it later (no answer, 429, or a 5xx), with
        waits of RETRY_S, then twice that. A POST is tried once unless `retry` says it only reads: posting a comment
        again after a lost answer could post it twice."""
        retry = method != "POST" if retry is None else retry
        for attempt in range(ATTEMPTS):
            try:
                return self._once(method, path, params, body, missing_ok)
            except GitHubError as e:
                if not retry or e.status not in RETRIED or attempt == ATTEMPTS - 1:
                    raise
                self._sleep(RETRY_S * 2 ** attempt)

    def _once(self, method: str, path: str, params: dict, body, missing_ok: bool):
        self._wait()
        url = f"{self.API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                   "User-Agent": "bug-registry-workflow", "X-GitHub-Api-Version": "2022-11-28"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as r:  # nosec B310: a fixed https URL
                raw = r.read()
                return r.status, (json.loads(raw.decode("utf-8")) if raw.strip() else None), \
                    r.headers.get("Link", "") or ""
        except urllib.error.HTTPError as e:
            with e:
                if missing_ok and e.code == 404:
                    return 404, None, ""
                raise GitHubError(f"{method} {path}: HTTP {e.code} {_message(e)}".rstrip(), status=e.code) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GitHubError(f"{method} {path}: {e}") from None

    def _pages(self, path: str, params: dict = None, limit: int = 20) -> list:
        out = []
        for page in range(1, limit + 1):
            _, data, link = self._call("GET", path, {**(params or {}), "per_page": 100, "page": page})
            out += data if isinstance(data, list) else []
            if 'rel="next"' not in link:
                break
        return out

    def _issues(self) -> str:
        return f"repos/{self.repo}/issues"

    def issue(self, number: int) -> dict:
        return self._call("GET", f"{self._issues()}/{int(number)}")[1] or {}

    def comments(self, number: int) -> list:
        return self._pages(f"{self._issues()}/{int(number)}/comments")

    def comment(self, comment_id: int) -> dict:
        """The comment, or {} once it is deleted."""
        return self._call("GET", f"{self._issues()}/comments/{int(comment_id)}", missing_ok=True)[1] or {}

    def create_comment(self, number: int, body: str) -> dict:
        return self._call("POST", f"{self._issues()}/{int(number)}/comments", body={"body": body})[1] or {}

    def edit_comment(self, comment_id: int, body: str) -> dict:
        return self._call("PATCH", f"{self._issues()}/comments/{int(comment_id)}", body={"body": body})[1] or {}

    def delete_comment(self, comment_id: int) -> None:
        self._call("DELETE", f"{self._issues()}/comments/{int(comment_id)}", missing_ok=True)

    def add_labels(self, number: int, labels: list) -> None:
        self._call("POST", f"{self._issues()}/{int(number)}/labels", body={"labels": list(labels)})

    def edit_issue(self, number: int, **fields) -> None:
        self._call("PATCH", f"{self._issues()}/{int(number)}", body=fields)

    def updated_comments(self, since: str) -> list:
        return self._pages(f"{self._issues()}/comments", {"since": since, "sort": "updated", "direction": "asc"})

    def find(self, fp: str) -> list:
        """The numbers of the registry issues whose body holds `fp`: only issues labeled devkit-bug, which only this
        workflow sets, so an issue that merely quotes a fingerprint is never taken for a bug's first issue."""
        query = f"repo:{self.repo} is:issue label:{bugs.LABEL} in:body {fp}"
        data = self._call("GET", "search/issues", {"q": query, "per_page": 100})[1]
        items = data.get("items") if isinstance(data, dict) else None
        return [n for n in map(bugs.registry_issue_number, items or []) if n]

    def recent_issues(self) -> list:
        params = {"state": "all", "sort": "created", "direction": "desc", "per_page": 50}
        data = self._call("GET", self._issues(), params)[1]
        return data if isinstance(data, list) else []

    def releases(self) -> list:
        return self._pages(f"repos/{self.repo}/releases", limit=3)

    def closed_issues(self, label: str, since: str) -> list:
        params = {"labels": label, "state": "closed", **({"since": since} if since else {})}
        return self._pages(self._issues(), params)

    def registry_issues(self, since: str) -> list:
        """Every open issue labeled devkit-bug, then the closed ones updated since `since`: the API filters on the
        update time, so the caller keeps those closed since then."""
        return self._pages(self._issues(), {"labels": bugs.LABEL, "state": "open"}) + \
            self.closed_issues(bugs.LABEL, since)

    def labels(self) -> list:
        return self._pages(f"repos/{self.repo}/labels")

    def create_label(self, name: str, color: str, description: str) -> None:
        self._call("POST", f"repos/{self.repo}/labels", body={"name": name, "color": color,
                                                                "description": description})

    def closure(self, number: int) -> dict:
        """Who closed the issue last (login and account id), and whether a merged pull request closed it. REST issue
        events do not link the pull request; GraphQL's ClosedEvent.closer does. Anything unreadable counts as not
        merged, and an account id it cannot read as 0."""
        owner, name = self.repo.split("/", 1)
        data = self._call("POST", "graphql", body={"query": CLOSURE_QUERY,
                                                   "variables": {"owner": owner, "name": name, "number": int(number)}},
                          retry=True)[1]
        node = ((((data or {}).get("data") or {}).get("repository") or {}).get("issue") or {})
        nodes = (node.get("timelineItems") or {}).get("nodes") or []
        last = nodes[-1] if nodes and isinstance(nodes[-1], dict) else {}
        closer = last.get("closer") if isinstance(last.get("closer"), dict) else {}
        actor = last.get("actor") if isinstance(last.get("actor"), dict) else {}
        return {"actor": str(actor.get("login") or ""), "actor_id": _number(actor.get("databaseId")),
                "merged_pr": closer.get("__typename") == "PullRequest" and closer.get("merged") is True}

    def comment_editors(self, node_id: str):
        """The accounts in the comment's public edit history ({"login", "type"}, oldest first; an account deleted
        since reads as {"login": "", "type": ""}), from GraphQL's userContentEdits, which REST does not serve. None
        when it cannot be told: no node id, no such comment, or more edits than one page holds."""
        if not isinstance(node_id, str) or not node_id:
            return None
        data = self._call("POST", "graphql", body={"query": EDITORS_QUERY, "variables": {"id": node_id}},
                          retry=True)[1]
        edits = (((data or {}).get("data") or {}).get("node") or {}).get("userContentEdits")
        nodes = edits.get("nodes") if isinstance(edits, dict) else None
        if not isinstance(nodes, list) or _number(edits.get("totalCount")) > len(nodes):
            return None
        out = []
        for n in nodes:
            editor = n.get("editor") if isinstance(n, dict) and isinstance(n.get("editor"), dict) else {}
            out.append({"login": str(editor.get("login") or ""), "type": str(editor.get("__typename") or "")})
        return out

    def is_member(self, login: str) -> bool:
        """The repository's owner, or a public member of the owning organization. The workflow's token cannot read
        private memberships; the maintainers file lists those members by login and account id."""
        owner = self.repo.split("/", 1)[0]
        if login.lower() == owner.lower():
            return True
        status, _, _ = self._call("GET", f"orgs/{owner}/public_members/{urllib.parse.quote(login, safe='')}",
                                  missing_ok=True)
        return status == 204


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: bug_registry_action.py <event file> <event name>", file=sys.stderr)
        sys.exit(2)
    try:
        github = GitHub()
    except GitHubError as e:
        print(f"bug registry: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], github))
