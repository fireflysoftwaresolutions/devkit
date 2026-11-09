#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bug registry's workflow: keep one bot-written record per registry issue.

`.github/workflows/bug-registry.yml` runs this on `issues: opened`, `issue_comment: created` and `release: published`
with two arguments, the event file GitHub writes and the event name. Event text is read from that file, never pasted
into a command.

- An issue's first payload is its body. A valid report makes the issue a registry issue: it gets the labels, a title
  generated from the report, and the record comment the bot writes. Anything else leaves the issue alone.
- Each run folds every payload comment it has not folded yet, oldest first, so a run cancelled by the concurrency
  group loses nothing. The record keeps `seen`, the highest comment id handled, and `folds`, today's folded payloads
  per account, which cap every account at DAILY_CAP payloads a day across the registry. Folded votes, confirmations
  and failure marks are deleted; detours and reports stay on the issue; anything invalid is left alone.
- A later issue for a fingerprint that already has one is closed as its duplicate. It keeps a small record of its own
  (status `duplicate`), and its payloads, now and later, are folded into the first issue's record.
- A published release sets `fixed_in` on the bugs closed as completed since the previous release by a merged pull
  request or by a member of the organization. A fixed bug that regresses (a member's vote, or three accounts', on a
  fixed version) is reopened and labeled.

Voters are HMAC-SHA256 digests of the GitHub account id, keyed with the repository secret BUG_REGISTRY_KEY, never
logins. Trust comes from GitHub's `author_association`: OWNER and MEMBER count. A member whose membership is private
reaches the workflow as CONTRIBUTOR or NONE, so `.github/bug-registry-maintainers` lists such accounts by login and
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
EVENTS = {"issues": "opened", "issue_comment": "created", "release": "published"}
TRIES = 3  # writes of one record that found it changed by another run, before the run gives up
WAITS, WAIT_S = 4, 15  # a duplicate waits up to a minute for the first issue's own run to write its record
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


class GitHubError(Exception):
    """A GitHub API call failed."""


class RegistryError(Exception):
    """The run cannot finish now; the next event on the issue folds what is left."""


class Changed(Exception):
    """A record comment changed between this run reading it and writing it."""


# --------------------------------------------------------------------------- reading issues and comments

def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _voter(key: bytes, user) -> str:
    """The keyed digest that stands for a GitHub account in a record, or '' for a user without an id."""
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    if isinstance(uid, bool) or not isinstance(uid, int):
        return ""
    return hmac.new(key, str(uid).encode(), hashlib.sha256).hexdigest()


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
    """The valid payload a body holds, or None."""
    p = bugs.parse_payload(body)
    return p if p is not None and not bugs.validate_payload(p) else None


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
    bot's first comment with the record marker, readable or not, so a broken one is rewritten rather than doubled."""
    comments = sorted((c for c in client.comments(number) or [] if isinstance(c, dict) and _number(c.get("id"))),
                      key=lambda c: c["id"])
    holder = next((c for c in comments if _by_bot(c) and bugs.RECORD_MARK in (c.get("body") or "")), None)
    return comments, holder, (bugs.parse_record(holder.get("body")) if holder else None)


def _write(client, number: int, holder, rec: dict):
    """Write the record when the comment's text differs from it: after a fold, or after a person edited the comment.
    The comment is read again just before; if it changed since this run read it, Changed says to start over."""
    body = bugs.render_record(rec)
    if holder is None:
        client.create_comment(number, body)
    elif holder.get("body") != body:
        if (client.comment(holder["id"]) or {}).get("body") != holder.get("body"):
            raise Changed(number)
        client.edit_comment(holder["id"], body)


# --------------------------------------------------------------------------- folding

def _entries(key: bytes, issue: dict, comments: list, rec, fp: str, maintainers: frozenset) -> list:
    """What this run folds, oldest first: the issue's body when it has no record yet, then every comment past `seen`
    holding a valid payload for this fingerprint. -> [(payload, voter, author_association, comment id or None)], with
    a listed maintainer's association as MEMBER."""
    out = []
    if rec is None:
        out.append((_first_report(issue), _voter(key, issue.get("user")),
                    _association(issue.get("user"), issue.get("author_association"), maintainers), None))
    seen = _number((rec or {}).get("seen"))
    for c in comments:
        if c["id"] <= seen or _by_bot(c):
            continue
        p = _payload(c.get("body"))
        if p is not None and p["fingerprint"] == fp:
            out.append((p, _voter(key, c.get("user")), _association(c.get("user"), c.get("author_association"),
                                                                       maintainers), c["id"]))
    return [e for e in out if e[0] is not None and e[1]]


def _used_today(client, now: float) -> dict:
    """{voter: payloads folded today} over every record the bot changed today."""
    day, used = _today(now), {}
    for c in client.updated_comments(f"{day}T00:00:00Z") or []:
        if not isinstance(c, dict) or not _by_bot(c):
            continue
        folds = (bugs.parse_record(c.get("body")) or {}).get("folds")
        if isinstance(folds, dict) and folds.get("day") == day and isinstance(folds.get("by"), dict):
            for who, n in folds["by"].items():
                if _number(n):
                    used[who] = used.get(who, 0) + n
    return used


def _fold_all(rec, entries: list, used: dict, now: float):
    """Fold the entries into `rec` in order, skipping the payloads of an account that reached DAILY_CAP today.
    -> (the record, the entries folded); `used` and the record's `folds` count them."""
    day = _today(now)
    folds = (rec or {}).get("folds")
    by = dict(folds["by"]) if isinstance(folds, dict) and folds.get("day") == day and \
        isinstance(folds.get("by"), dict) else {}
    done = []
    for entry in entries:
        payload, who, assoc, _ = entry
        if used.get(who, 0) >= DAILY_CAP:
            continue
        new = bugs.fold(rec, payload, author_hash=who, author_assoc=assoc, now=now)
        if not new:
            continue  # a record starts only with a report
        rec = new
        used[who] = used.get(who, 0) + 1
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
    for payload, _, _, cid in done:
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


def _fold_into(client, target: int, entries: list, now: float) -> list:
    """Fold a duplicate's payloads into the first issue's record. -> the entries folded."""
    for _ in range(TRIES):
        _, holder, rec = _read(client, target)
        if rec is None or rec.get("status") == "duplicate":
            raise RegistryError(f"issue #{target} has no record to fold a duplicate into")
        new, done = _fold_all(rec, entries, _used_today(client, now), now)
        try:
            _write(client, target, holder, new)
        except Changed:
            continue
        _regressed(client, target, rec, new)
        return done
    raise RegistryError(f"the record of issue #{target} kept changing while this run wrote it")


def _forward(client, number: int, issue: dict, holder, stub: dict, entries: list, seen: int, now: float) -> str:
    """A duplicate's payloads go to the first issue; the duplicate keeps its stub record, so nothing is folded twice,
    has no labels, and is closed once with a pointer to the first issue."""
    target = _number(stub.get("duplicate_of"))
    if not target or target == number:
        raise RegistryError(f"the duplicate record of issue #{number} names no other issue")
    done = _fold_into(client, target, entries, now) if entries else []
    _write(client, number, holder, {**stub, "seen": seen})
    _delete(client, done)
    if issue.get("state") != "closed":
        client.edit_issue(number, state="closed", state_reason="not_planned")
        client.create_comment(number, f"Duplicate of #{target}. Reports and votes for this bug count there.")
    return f"#{number} duplicates #{target}: {len(done)} payload(s) moved"


def _fold_issue_once(client, key: bytes, number: int, now: float, sleep, maintainers: frozenset) -> str:
    issue = client.issue(number) or {}
    if issue.get("pull_request"):
        return f"#{number} is a pull request: skipped"
    comments, holder, rec = _read(client, number)
    first = _first_report(issue) if rec is None else None
    if rec is None and first is None:
        return f"#{number} has no valid first payload: left alone"
    fp = (rec or first)["fingerprint"]
    entries = _entries(key, issue, comments, rec, fp, maintainers)
    seen = max([c["id"] for c in comments] + [_number((rec or {}).get("seen"))])
    if rec is None:
        target = _first_issue(client, fp, number, sleep)
        if target is not None:
            rec = {"devkit_bug": bugs.SCHEMA_VERSION, "fingerprint": fp, "status": "duplicate", "duplicate_of": target}
    if rec is not None and rec.get("status") == "duplicate":
        return _forward(client, number, issue, holder, rec, entries, seen, now)
    new, done = _fold_all(rec, entries, _used_today(client, now) if entries else {}, now)
    if not new:
        return f"#{number}: its first payload is past its author's daily cap; the next event folds it"
    new["seen"] = seen
    _write(client, number, holder, new)
    missing = [lb for lb in (bugs.LABEL, f"kind:{new['kind']}") if lb not in _labels(issue)]
    if missing:
        client.add_labels(number, missing)
    if rec is None:
        title = bugs.TITLE_PREFIX + bugs.title_for(first)
        if issue.get("title") != title:
            client.edit_issue(number, title=title)
    _regressed(client, number, rec, new, issue)
    _delete(client, done)
    return f"#{number}: {len(done)} payload(s) folded"


def _fold_issue(client, key: bytes, number: int, now: float, sleep, maintainers: frozenset) -> str:
    for _ in range(TRIES):
        try:
            return _fold_issue_once(client, key, number, now, sleep, maintainers)
        except Changed:
            continue
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


# --------------------------------------------------------------------------- releases

def _mark_fixed(client, number: int, version: str) -> bool:
    for _ in range(TRIES):
        _, holder, rec = _read(client, number)
        if rec is None or rec.get("status") == "duplicate":
            return False
        old = core.version_key(rec.get("fixed_in"))
        if old and old >= core.version_key(version) and rec.get("status") != "regressed":
            return False
        try:
            _write(client, number, holder, {**rec, "fixed_in": version, "status": "fixed", "regression": []})
        except Changed:
            continue
        return True
    raise RegistryError(f"the record of issue #{number} kept changing while this run wrote it")


def _release(client, event: dict, maintainers: frozenset) -> str:
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
        if _mark_fixed(client, number, version):
            marked.append(number)
    return f"release {version}: {len(marked)} bug(s) marked fixed" + \
        (" (" + ", ".join(f"#{n}" for n in marked) + ")" if marked else "")


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
    if EVENTS.get(event_name) != event.get("action"):
        print("bug registry: not an event the registry handles: skipped")
        return 0
    if event_name != "release" and bugs.LABEL not in _labels(issue) and _first_report(issue) is None:
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
            message = _release(client, event, listed)
        else:
            number = _number(issue.get("number"))
            if not number:
                return _fail("the event names no issue", 1)
            message = _fold_issue(client, key.encode(), number, now, sleep, listed)
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

    def __init__(self, repo: str = None, token: str = None, timeout: float = 30, opener=None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise GitHubError("GITHUB_REPOSITORY must name the repository as owner/name")
        if not self.token:
            raise GitHubError("GITHUB_TOKEN is not set")
        self.timeout, self._open = timeout, opener or urllib.request.urlopen

    def _call(self, method: str, path: str, params: dict = None, body=None, missing_ok: bool = False):
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
                raise GitHubError(f"{method} {path}: HTTP {e.code} {_message(e)}".rstrip()) from None
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
        return self._call("GET", f"{self._issues()}/comments/{int(comment_id)}")[1] or {}

    def create_comment(self, number: int, body: str) -> dict:
        return self._call("POST", f"{self._issues()}/{int(number)}/comments", body={"body": body})[1] or {}

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._call("PATCH", f"{self._issues()}/comments/{int(comment_id)}", body={"body": body})

    def delete_comment(self, comment_id: int) -> None:
        self._call("DELETE", f"{self._issues()}/comments/{int(comment_id)}", missing_ok=True)

    def add_labels(self, number: int, labels: list) -> None:
        self._call("POST", f"{self._issues()}/{int(number)}/labels", body={"labels": list(labels)})

    def edit_issue(self, number: int, **fields) -> None:
        self._call("PATCH", f"{self._issues()}/{int(number)}", body=fields)

    def updated_comments(self, since: str) -> list:
        return self._pages(f"{self._issues()}/comments", {"since": since, "sort": "updated", "direction": "asc"})

    def find(self, fp: str) -> list:
        data = self._call("GET", "search/issues", {"q": f"repo:{self.repo} is:issue in:body {fp}", "per_page": 100})[1]
        return [i["number"] for i in (data or {}).get("items") or [] if isinstance(i, dict) and _number(i.get("number"))]

    def recent_issues(self) -> list:
        params = {"state": "all", "sort": "created", "direction": "desc", "per_page": 50}
        data = self._call("GET", self._issues(), params)[1]
        return data if isinstance(data, list) else []

    def releases(self) -> list:
        return self._pages(f"repos/{self.repo}/releases", limit=3)

    def closed_issues(self, label: str, since: str) -> list:
        params = {"labels": label, "state": "closed", **({"since": since} if since else {})}
        return self._pages(self._issues(), params)

    def closure(self, number: int) -> dict:
        """Who closed the issue last (login and account id), and whether a merged pull request closed it. REST issue
        events do not link the pull request; GraphQL's ClosedEvent.closer does. Anything unreadable counts as not
        merged, and an account id it cannot read as 0."""
        owner, name = self.repo.split("/", 1)
        data = self._call("POST", "graphql", body={"query": CLOSURE_QUERY,
                                                   "variables": {"owner": owner, "name": name, "number": int(number)}})[1]
        node = ((((data or {}).get("data") or {}).get("repository") or {}).get("issue") or {})
        nodes = (node.get("timelineItems") or {}).get("nodes") or []
        last = nodes[-1] if nodes and isinstance(nodes[-1], dict) else {}
        closer = last.get("closer") if isinstance(last.get("closer"), dict) else {}
        actor = last.get("actor") if isinstance(last.get("actor"), dict) else {}
        return {"actor": str(actor.get("login") or ""), "actor_id": _number(actor.get("databaseId")),
                "merged_pr": closer.get("__typename") == "PullRequest" and closer.get("merged") is True}

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
