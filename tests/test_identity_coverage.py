# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# identity.STATE_CHANGING is the set of commands a call token decides and, in a session whose calls carry tokens,
# nothing else does. This test walks dev's parser (cli.build_parser, read, never edited) over the commands that
# coordinate work (progress, crew, session, task, plan) and runs every subcommand twice, each time in a fresh
# fixture: once with devkit's identity lookups (acting_lane, require, verified_lane, progress.caller) stopping the
# command where it asks who calls, and once with them naming the tech lead, to see whether the command changes the
# run, the session record, the work items, the plans or the crew's records. A subcommand that changes any of them
# must be in STATE_CHANGING; one that only asks who calls must be in identity.READS; every name in either must be a
# subcommand dev has.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_coverage -v
import argparse
import contextlib
import hashlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import cli, core, identity, peers, plans, work  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

FAMILIES = ("progress", "crew", "session", "task", "plan")
# what each subcommand runs with, as the tech lead would type it; {M}, {D}, {T} and {P} are the fixture's message,
# decision, work item and plan. A subcommand dev gains without a line here fails the test until it gets one.
CASES = {
    ("progress", "start"): ["Second run", "--replace", "--why", "a new goal", "--plan", "a; b"],
    ("progress", "note"): ["still working"],
    ("progress", "step"): ["docs written", "--step", "docs", "--check", "✓ read it twice"],
    ("progress", "block"): ["waiting for the API key"],
    ("progress", "done"): ["finished", "--why", "the test's end"],
    ("progress", "assign"): ["docs", "--to", "Ghost"],
    ("progress", "begin"): ["docs"],
    ("progress", "add"): ["extra=10m"],
    ("progress", "reopen"): ["docs", "--why", "redo"],
    ("progress", "skip"): ["docs", "--why", "not needed"],
    ("progress", "confirm"): ["docs", "--why", "still holds"],
    ("progress", "history"): [],
    ("progress", "time"): [],
    ("crew", "raci"): ["backend", "--set", "R=tank A=seraph C=architect I=ghost", "--why", "a test"],
    ("crew", "set"): ["tank", "summary", "Builds APIs", "--why", "shorter"],
    ("crew", "history"): [],
    ("crew", "rules"): [],
    ("crew", "graph"): [],
    ("crew", "show"): ["tank"],
    ("crew", "brief"): ["tank", "Add", "the", "export", "--step", "parser"],
    ("crew", "sync"): None,  # writes devkit's generated agent files into its own checkout: never run here
    ("crew", "ask"): ["Is the parser done?"],
    ("crew", "answer"): ["{M}", "yes"],
    ("crew", "read"): ["{M}", "--json"],
    ("crew", "ack"): ["{M}", "--sha256", "{M_SHA}", "--generation", "1"],
    ("crew", "inbox"): [],
    ("crew", "mode"): ["auto"],
    ("crew", "decide"): ["{D}", "yes"],
    ("crew", "withdraw"): ["{D}", "--why", "no longer applies"],
    ("crew", "decisions"): [],
    ("crew", "route"): ["--plan", "--apply"],
    ("crew", "delegate"): ["review", "the", "parser", "--to", "Seraph"],
    ("crew", "decline"): ["parser", "--why", "not mine"],
    ("crew", "owner"): ["lib/x.py"],
    ("crew", "next"): ["--first", "docs", "--why", "the developer asked for it first"],
    ("session", "brief"): [],
    ("session", "carry"): [],
    ("session", "compactions"): ["{TRANSCRIPT}"],
    ("session", "forget"): ["--session", "claude:s1"],
    ("session", "ls"): [],
    ("session", "tell"): ["@all", "the parser is done"],
    ("session", "ask"): ["@all", "is anyone on the parser?"],
    ("session", "answer"): ["Q1", "yes"],
    ("session", "inbox"): [],
    ("session", "read"): ["S1"],
    ("session", "ack"): ["S1", "--sha256", "0" * 64, "--generation", "1"],
    ("session", "claim"): ["release:acme/api", "--why", "releasing"],
    ("session", "release"): ["release:acme/api"],
    ("session", "claims"): [],
    ("session", "escalate"): ["@all", "who releases?", "--options", "A: me; B: you", "--recommend", "A"],
    ("session", "escalations"): [],
    ("session", "replay"): [],
    ("task", "add"): ["Write", "the", "docs", "-p", "acme/api"],
    ("task", "start"): ["{T}", "-p", "acme/api"],
    ("task", "claim"): ["{T}", "-p", "acme/api"],
    ("task", "release"): ["{T}", "-p", "acme/api"],
    ("task", "done"): ["{T}", "--why", "shipped", "-p", "acme/api"],
    ("task", "block"): ["{T}", "--why", "waits on the API", "-p", "acme/api"],
    ("task", "unblock"): ["{T}", "--why", "the API is up", "-p", "acme/api"],
    ("task", "cancel"): ["{T}", "--why", "not needed", "-p", "acme/api"],
    ("task", "reopen"): ["{T}", "--why", "again", "-p", "acme/api"],
    ("task", "show"): ["{T}", "-p", "acme/api"],
    ("task", "history"): ["{T}", "-p", "acme/api"],
    ("task", "link"): ["{T}", "--to", "https://example.com/issue/1", "-p", "acme/api"],
    ("task", "delegate"): ["{T}", "--to", "ada", "--why", "her area", "-p", "acme/api"],
    ("task", "move"): ["{T}", "--to", "now", "--why", "urgent", "-p", "acme/api"],
    ("task", "edit"): ["{T}", "--title", "Fix the login timeout now", "--why", "clearer", "-p", "acme/api"],
    ("task", "reconcile"): ["-p", "acme/api"],
    ("task", "ls"): ["-p", "acme/api"],
    ("plan", "new"): ["Export", "invoices", "-p", "acme/api"],
    ("plan", "revise"): ["{P}", "--why", "a new step"],
    ("plan", "approve"): ["{P}"],
    ("plan", "supersede"): ["{P}", "--why", "replaced"],
    ("plan", "ls"): ["-p", "acme/api"],
}
# listed commands that find nothing to change in the fixture: no continuity records to forget (a session's brief
# writes them later), no message waiting to be sent again
NOTHING_TO_DO = {("session", "forget"), ("session", "replay")}
LOOKUPS = ("acting_lane", "require", "verified_lane")
QUIET = ("audit", "logs", "timing", "task-lists", "captures", "bugs", "crashes", "prompt-state", "continuity")
LOCK = re.compile(r"\.lock\Z|\A[0-9a-f]{32}-\d+\Z")  # a record's lock and its holder's mark, not a record


class _Asked(BaseException):
    # raised where a command asks who calls, so it stops there and changes nothing
    pass


def _subparsers(p):
    return next((a.choices for a in p._actions if isinstance(a, argparse._SubParsersAction)), {})


def subcommands() -> set:
    # every (command, subcommand) of the coordination commands, as dev's parser has them: its subparsers, or the
    # choices of its first positional (dev session brief|carry|…)
    top, out = _subparsers(cli.build_parser()), set()
    for fam in FAMILIES:
        subs = _subparsers(top[fam])
        if subs:
            out |= {(fam, name) for name in subs}
        else:
            first = next(a for a in top[fam]._actions if not a.option_strings and a.choices)
            out |= {(fam, name) for name in first.choices}
    return out


class Coordination:
    # a run with a member at work, a message to the tech lead, a decision waiting, a work item and a plan
    def __enter__(self):
        self.state = TempState(real_caller=True).__enter__()
        self.root = Path(self.state.tmp.name)
        handoff = self.root / "handoff"
        self.patches = [mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(work, "HANDOFF", handoff),
                        mock.patch.object(work, "ITEMS", handoff / "items"),
                        mock.patch.object(work, "_commit", lambda *a, **k: None),
                        mock.patch.object(plans, "HANDOFF", handoff), mock.patch.object(plans, "PLANS",
                                                                                        handoff / "plans"),
                        # the work items' project, which no registry on this test's Mac knows
                        mock.patch.object(cli, "_scope", lambda a: ("acme/api", "acme/api"))]
        for p in self.patches:
            p.start()
        main = (mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}),
                mock.patch.object(identity, "acting_lane", lambda *a, **k: "main"))
        with main[0], main[1]:
            pg.start("Work", plan=["parser", "docs"])
            pg.assign("parser", "Tank", why="the test's assignment")
            self.decision = pg.crew_decision("Ship the parser now?", options="yes|no")["id"]
        launch("a_tank", "devkit:tank", "Tank: parser")
        msg = pg.crew_ask("Report: the parser is done", from_agent="a_tank")
        self.message = msg["id"]
        self.message_sha = msg.get("body_sha256") or hashlib.sha256(b"").hexdigest()
        self.task = work.create("acme/api", "Fix the login timeout", kind="bug", why="users are logged out")["meta"][
            "id"]
        self.plan = str(plans.new("acme/api", "Invoice export"))
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text("")
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        self.state.__exit__(*exc)

    def argv(self, fam, name):
        fill = {"{M}": self.message, "{M_SHA}": self.message_sha, "{D}": self.decision, "{T}": self.task,
                "{P}": self.plan, "{TRANSCRIPT}": str(self.transcript)}
        return [fam, name] + [fill.get(w, w) for w in CASES[(fam, name)]]

    def snapshot(self) -> dict:
        # every record a coordination command can change: runs, session records, the call ledger, work items, plans
        # and the crew's records; devkit's own logs and the audit trail are left out
        out = {}
        for top in (self.root, Path(core.STATE), peers.PEERS):  # and the records between sessions on this Mac
            for f in top.rglob("*") if top.is_dir() else []:
                rel = f.relative_to(top)
                if f.is_file() and not any(part in QUIET for part in rel.parts) and not LOCK.search(f.name):
                    out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out


def run(fam, name, asked: list):
    # the subcommand once, in its own fixture -> (exit code, whether it changed a record); `asked` gets the lookups
    # it reached (each stops the command when `asked` is not None)
    with Coordination() as fx:
        argv, before = fx.argv(fam, name), fx.snapshot()
        out = io.StringIO()

        def stop(what):
            def f(*a, **k):
                asked.append(what)
                raise _Asked(what)
            return f
        lookups = [mock.patch.object(identity, k, stop(k)) for k in LOOKUPS] + [
            mock.patch.object(pg, "caller", stop("progress.caller"))] if asked is not None else [
            mock.patch.object(identity, "acting_lane", lambda *a, **k: "main"),
            mock.patch.object(identity, "speaker", lambda *a, **k: identity.Speaker("claude", "s1", "main", "test", True)),
            mock.patch.object(identity, "require", lambda sp, **k: sp),
            mock.patch.object(identity, "verified_lane", lambda lane, *a, **k: lane),
            mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test", "agent": "claude",
                                                             "session": "s1", "calls": []})]
        with contextlib.ExitStack() as stack:
            for p in lookups:
                stack.enter_context(p)
            stack.enter_context(mock.patch.object(sys, "argv", ["dev", *argv]))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            try:
                rc = cli.main(argv)
            except _Asked:
                rc = "asked"
            except SystemExit as e:
                rc = e.code
        after = fx.snapshot()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        return rc, changed, out.getvalue()


class StateChangingTests(unittest.TestCase):
    def test_the_listed_commands_are_dev_subcommands(self):
        listed = {(fam, sub) for fam, subs in identity.STATE_CHANGING.items() for sub in subs} | \
                 {(fam, sub) for fam, subs in identity.READS.items() for sub in subs}
        self.assertEqual(sorted(listed - subcommands()), [])
        both = {(f, s) for f, subs in identity.STATE_CHANGING.items() for s in subs
                if s in identity.READS.get(f, ())}
        self.assertEqual(sorted(both), [])

    def test_a_subcommand_that_asks_who_calls_or_changes_state_is_listed(self):
        self.assertEqual(sorted(subcommands() ^ set(CASES)), [])  # every subcommand dev has, and only those, has a case
        missing, wrong = [], []
        for fam, name in sorted(subcommands()):
            if CASES[(fam, name)] is None:
                continue
            with self.subTest(f"dev {fam} {name}"):
                asked = []
                run(fam, name, asked)
                rc, changed, out = run(fam, name, None)
                changes = name in identity.STATE_CHANGING.get(fam, ())
                reads = name in identity.READS.get(fam, ())
                if changed and not changes:
                    missing.append(f"dev {fam} {name} changes state ({', '.join(Path(c).name for c in changed[:3])})")
                if asked and not changed and not (changes or reads):
                    missing.append(f"dev {fam} {name} asks who calls ({asked[0]})")
                if changes and not changed and rc == 0 and (fam, name) not in NOTHING_TO_DO:  # it ran, and only read
                    wrong.append(f"dev {fam} {name} changed nothing: {out.strip()[-160:]}")
        self.assertEqual(missing + wrong, [])


if __name__ == "__main__":
    unittest.main()
