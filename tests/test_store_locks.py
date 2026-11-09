# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Stores that several devkit processes on one Mac change at once: agent sessions (Claude Code and Codex) and the
# scheduled jobs. Each test starts real Python processes with a temporary HOME of their own (so devkit's state,
# configuration and handoff repository are the test's), lets them change one store at the same moment (each waits
# for a shared go-file once it is ready), and checks that every process succeeded and no record was lost.
# The stand-ins of tests/_sandbox.py stay first on PATH, so no real claude, codex or gh ever runs.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_store_locks
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from textwrap import dedent

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))
import _sandbox  # noqa: E402

TIMEOUT = 60  # seconds one process may take

# Every process: devkit importable, the test's arguments in ARGS, its setup, then it says it is ready and waits for
# the go-file, so the processes of a test start their changes at the same moment.
HEAD = dedent("""\
    import json, os, sys, time
    sys.path.insert(0, {lib!r})
    from pathlib import Path
    DIR, WHO, ARGS = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
    """)
BARRIER = dedent("""
    (DIR / ("ready-" + WHO)).touch()
    while not (DIR / "go").exists():
        time.sleep(0.001)
    """)


def code(*parts) -> str:
    # Program text from parts written indented in a test: each part dedented on its own.
    return "\n".join(dedent(p) for p in parts)


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="devkit-store-locks-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.home = self.dir / "home"
        self.state = self.home / ".local" / "state" / "devkit"
        self.handoff = self.home / "Development" / ".handoff"
        self.stubs = []
        self.config({"github_user": "tester"})

    def config(self, cfg: dict):
        f = self.home / ".config" / "devkit" / "config.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cfg))

    def race(self, procs) -> dict:
        # procs: [(who, setup, body, args)]. Starts them all, waits until each is ready, then lets them go together.
        # -> {who: stdout}. A process that fails or runs past TIMEOUT fails the test; all have ended on return.
        for f in list(self.dir.glob("go")) + list(self.dir.glob("ready-*")):
            f.unlink()
        running = []
        try:
            for who, setup, body, args in procs:
                program = HEAD.format(lib=str(TREE / "lib")) + code(setup) + BARRIER + code(body)
                running.append((who, subprocess.Popen(
                    [sys.executable, "-I", "-c", program, str(self.dir), who, json.dumps(args)], cwd=str(self.dir),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
                    env=_sandbox.env(stubs=self.stubs, HOME=str(self.home)))))
            for who, p in running:
                end = time.monotonic() + TIMEOUT
                while not (self.dir / f"ready-{who}").exists():
                    if p.poll() is not None or time.monotonic() > end:
                        p.kill()
                        so, se = p.communicate(timeout=5)
                        self.fail(f"{who} never got ready (exit {p.returncode})\n{so}\n{se[-2000:]}")
                    time.sleep(0.002)
            (self.dir / "go").touch()
            out = {}
            for who, p in running:
                so, se = p.communicate(timeout=TIMEOUT)
                self.assertEqual(p.returncode, 0, f"{who} failed:\n{so}\n{se[-2000:]}")
                out[who] = so
            return out
        finally:
            for _, p in running:
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def run_one(self, body: str, args=None) -> str:
        # One process on its own: sets a store up or reads it through devkit's own functions. -> its stdout.
        return self.race([("one", "", body, args or {})])["one"]


class AiBudgetTests(StoreCase):
    def test_the_daily_ai_budget_holds_and_counts_every_call_when_processes_ask_at_once(self):
        # Four background jobs ask the headless agent 15 times each against a budget of 30 calls a day: exactly 30
        # calls run, and the usage record counts all 30 (before, a call read the count while another rewrote it, so
        # counts were lost and the budget was overrun).
        self.config({"github_user": "tester", "ai": {"enabled": True, "agent": "claude", "daily_calls": 30,
                                                     "timeout": 30}})
        stubs = self.dir / "stubs"
        stubs.mkdir()
        (stubs / "claude").write_text('#!/bin/sh\ncat >/dev/null\necho call >> "$HOME/claude-calls"\necho yes\n')
        (stubs / "claude").chmod(0o755)
        self.stubs = [stubs]
        body = """
            from devkit import ai
            for i in range(15):
                ai.ask("Do these two lessons describe the same mistake?", "store lock test")
            """
        self.race([(f"job{n}", "", body, {}) for n in range(4)])
        calls = (self.home / "claude-calls").read_text().splitlines()
        self.assertEqual(len(calls), 30, "calls that ran against a budget of 30")
        self.assertEqual(self.run_one("from devkit import ai\nprint(ai.used())").strip(), "30",
                         "calls the usage record counted")


class RegistryTests(StoreCase):
    def test_sizes_measured_by_processes_at_once_are_all_kept(self):
        # `dev du` and the sync job store sizes in one cache at the same time: every measurement is kept.
        body = """
            from devkit import registry
            for i in range(60):
                registry.remember_sizes({"/sizes/%s/%d" % (WHO, i): i + 1})
            """
        self.race([(f"du{n}", "", body, {}) for n in range(4)])
        cache = json.loads((self.state / "cache" / "sizes.json").read_text())
        want = {f"/sizes/du{n}/{i}": i + 1 for n in range(4) for i in range(60)}
        self.assertEqual({k: v[0] for k, v in cache.items()}, want)

    def test_aliases_set_by_sessions_at_once_are_all_kept(self):
        # Four sessions give 15 projects each an alias at the same moment: the team's alias file keeps all 60.
        whos = [f"s{n}" for n in range(4)]
        projects = [{"id": f"acme/{w}-p{i}", "name": f"{w}-p{i}"} for w in whos for i in range(15)]
        body = """
            from devkit import registry
            for i in range(15):
                registry.set_alias("%sx%d" % (WHO, i), "acme/%s-p%d" % (WHO, i), ARGS["projects"], by="tester")
            """
        self.race([(w, "", body, {"projects": projects}) for w in whos])
        out = self.run_one("from devkit import registry\n"
                           "print(json.dumps({a: r['project'] for a, r in registry.load_aliases().items()}))")
        self.assertEqual(json.loads(out), {f"{w}x{i}": f"acme/{w}-p{i}" for w in whos for i in range(15)})


NOTE_PROJECT = """
    from devkit import branches, registry
    PID = "acme/widget"
    P = {"id": PID, "path": str(Path(os.environ["HOME"]) / "Development" / "acme" / "widget"), "canonical_ok": True,
         "stack": [], "commands": ["make test"], "main": None, "worktrees": []}
    """


class NoteTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.run_one(code(NOTE_PROJECT, "registry.update_note(P)"))
        self.note = self.handoff / "projects" / "acme" / "widget.md"

    def test_a_session_start_rewriting_the_note_keeps_an_embargo_set_meanwhile(self):
        # A session start regenerates the project's note (its auto block) while the developer embargoes a branch
        # in the same note: the embargo survives. The session start holds its write until the embargo is in or
        # 1.5 s pass, so before the fix the embargo landed between its read and its write and was written over.
        session_start = code(NOTE_PROJECT, """
            real = registry.write_if_changed

            def held(path, text):
                (DIR / "note-read").touch()
                end = time.time() + 1.5
                while not (DIR / "embargoed").exists() and time.time() < end:
                    time.sleep(0.005)
                return real(path, text)
            registry.write_if_changed = held
            P["commands"] = ["make check"]
            """)
        embargo = """
            end = time.time() + 30
            while not (DIR / "note-read").exists() and time.time() < end:
                time.sleep(0.002)
            branches.set_embargo(PID, "fix/cve-2026-1", why="unshipped security fix", who="tester")
            (DIR / "embargoed").touch()
            """
        out = self.race([("session", session_start, "print(registry.update_note(P))", {}),
                         ("embargo", NOTE_PROJECT, embargo, {})])
        self.assertEqual(out["session"].strip(), "True")
        embargoed = self.run_one(code(NOTE_PROJECT, "print(json.dumps(sorted(branches.embargoes(PID))))"))
        self.assertEqual(json.loads(embargoed), ["fix/cve-2026-1"], "the embargo was written over")
        self.assertIn("`make check`", self.note.read_text(), "the session start's auto block is missing")

    def test_dev_sync_regenerating_notes_under_the_notes_lock_does_not_wait_on_itself(self):
        # `dev sync` holds the notes lock around its note writes, which take it again: that inner take must not
        # wait for the outer one (a lock that is not reentrant waits its whole timeout, then skips the note).
        body = """
            P["commands"] = ["make sync"]
            start = time.time()
            with registry.notes_lock(wait=10) as got:
                wrote = registry.update_note(P)
            print(got, wrote, round(time.time() - start, 1))
            """
        got, wrote, took = self.race([("sync", NOTE_PROJECT, body, {})])["sync"].split()
        self.assertEqual((got, wrote), ("True", "True"))
        self.assertLess(float(took), 2.0, "seconds the note write waited")
        self.assertIn("`make sync`", self.note.read_text())


class LogTrimTests(StoreCase):
    def test_a_dev_rm_record_written_while_sleep_trims_the_trash_manifest_is_kept(self):
        # `dev rm` records 300 trashed items while the nightly sleep trims the manifest again and again: every
        # record stays, so `dev rm --undo` can restore each item, and the trim never fails on a half-written line.
        record = """
            from devkit import fs
            for i in range(300):
                fs._record([{"ts": time.time(), "batch": "b%d" % i, "op": "rm", "path": "/w/item-%d" % i}])
            (DIR / "recorded").touch()
            """
        trim = """
            from devkit import sleep
            f = sleep.STATE / "trash.jsonl"
            rounds = 0
            while not (DIR / "recorded").exists() and rounds < 5000:
                if f.exists():
                    sleep.apply_hygiene([("trash-manifest", f, "")])
                rounds += 1
            print(rounds)
            """
        out = self.race([("rm", "", record, {}), ("sleep", "", trim, {})])
        self.assertGreater(int(out["sleep"]), 1, "trims that ran while dev rm recorded")
        paths = [json.loads(ln)["path"] for ln in (self.state / "trash.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(paths), sorted(f"/w/item-{i}" for i in range(300)))

    def test_hook_log_lines_written_while_sleep_trims_the_hooks_log_are_kept(self):
        # Hooks log 3,000 lines while the nightly sleep trims the hooks log (over 1 MB: it keeps the newest 1 MB)
        # again and again: every hook line is in the log afterwards.
        log = self.state / "logs" / "hooks.log"
        log.parent.mkdir(parents=True)
        log.write_text("".join(f"2026-11-01 03:00 filler line {i:07d} {'x' * 40}\n" for i in range(20000)))
        hooks = """
            from devkit import hooks
            for i in range(3000):
                hooks._log("store-lock-test line %d" % i)
            (DIR / "logged").touch()
            """
        trim = """
            from devkit import sleep
            f = sleep.STATE / "logs" / "hooks.log"
            rounds = 0
            while not (DIR / "logged").exists() and rounds < 5000:
                sleep.apply_hygiene([("hooks-log", f, "")])
                rounds += 1
            print(rounds)
            """
        out = self.race([("hook", "", hooks, {}), ("sleep", "", trim, {})])
        self.assertGreater(int(out["sleep"]), 1, "trims that ran while the hooks logged")
        got = [ln.split("store-lock-test line ", 1)[1] for ln in log.read_text().splitlines()
               if "store-lock-test line " in ln]
        self.assertEqual(sorted(got, key=int), [str(i) for i in range(3000)])


class JobConfigTests(StoreCase):
    def test_job_schedules_changed_by_processes_at_once_all_reach_the_config(self):
        # Four processes change four jobs' schedules 20 times each at the same moment (dev jobs set): the config
        # ends with each job's last schedule and keeps the developer's other settings. launchd is never touched.
        names = ["sync", "repair", "gc", "sleep"]
        body = """
            from devkit import jobs
            jobs.install = lambda quiet=False: []
            for i in range(20):
                jobs.set_schedule(ARGS["job"], every="%dm" % (10 + i), why="store lock test")
            """
        self.race([(f"set{n}", "", body, {"job": name}) for n, name in enumerate(names)])
        cfg = json.loads((self.home / ".config" / "devkit" / "config.json").read_text())
        self.assertEqual({n: ((cfg.get("jobs") or {}).get(n) or {}).get("every") for n in names},
                         {n: 29 for n in names}, "each job's last schedule")
        self.assertEqual(cfg.get("github_user"), "tester", "the developer's other settings")


BUGS = """
    from devkit import bugs
    bugs.private_words = lambda login: frozenset()
    """


class BugStoreTests(StoreCase):
    def test_occurrences_recorded_while_two_trims_run_are_all_kept(self):
        # Three sessions record 150 devkit failures each while two `dev sleep` runs trim the occurrence log: every
        # recording succeeds and every occurrence's one-time code is still in the log (a marker naming it must
        # match).
        record = """
            codes = []
            for i in range(150):
                rec = bugs.record_occurrence("hook", "0123456789ab", impact="minor", session=WHO)
                codes.append(rec["code"] if rec["saved"] else "unsaved")
            print(json.dumps(codes))
            (DIR / ("recorded-" + WHO)).touch()
            """
        trim = """
            path = bugs.BUGS / "occurrences.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            rounds = 0
            while not all((DIR / ("recorded-s%d" % n)).exists() for n in range(3)) and rounds < 5000:
                with open(path, "a") as fh:  # an occurrence 100 days old, for the trim to remove
                    fh.write(json.dumps({"ts": time.time() - 100 * 86400, "code": "old"}) + "\\n")
                bugs.trim()
                rounds += 1
            print(rounds)
            """
        out = self.race([(f"s{n}", BUGS, record, {}) for n in range(3)] +
                        [(f"sleep{n}", BUGS, trim, {}) for n in range(2)])
        codes = [c for n in range(3) for c in json.loads(out[f"s{n}"])]
        self.assertNotIn("unsaved", codes)
        log = (self.state / "bugs" / "occurrences.jsonl").read_text().splitlines()
        self.assertEqual(sorted(set(codes) - {json.loads(ln).get("code") for ln in log}), [],
                         "occurrences the trims lost")

    def test_approvals_given_at_once_are_all_kept(self):
        # Four sessions each queue 20 held bug reports, then approve them at the same moment: all 80 approvals are
        # kept, so each report can go out.
        queue = code(BUGS, """
            ids = [bugs.queue({"payload": {"devkit_bug": 1, "type": "report", "version": "26.11.7",
                                           "fingerprint": "%012x" % (ARGS["n"] * 1000 + i)}, "held": True})
                   for i in range(20)]
            """)
        body = """
            for item_id in ids:
                bugs.approve(item_id)
            print(json.dumps(ids))
            """
        out = self.race([(f"s{n}", queue, body, {"n": n}) for n in range(4)])
        ids = sorted(i for o in out.values() for i in json.loads(o))
        self.assertEqual(len(set(ids)), 80)
        pending = json.loads(self.run_one(code(BUGS, "print(json.dumps({p['id']: p['approved'] "
                                                     "for p in bugs.pending()}))")))
        self.assertEqual(sorted(i for i, ok in pending.items() if ok), ids)

    def test_a_report_dropped_while_its_send_fails_stays_dropped(self):
        # The developer drops a queued report (dev bug drop) while the sync is sending it and GitHub fails: the
        # report stays dropped. Before, the failed send wrote the item back with its next retry time.
        item = self.run_one(code(BUGS, """
            bugs.set_consent("on", "tester")
            print(bugs.queue({"payload": {"devkit_bug": 1, "type": "report", "fingerprint": "abcdefabcdef",
                                          "version": "26.11.7", "impact": "blocked", "kind": "crash",
                                          "command": "progress step", "exc_class": "KeyError",
                                          "frames": ["work.actor_str"]}}))
            """)).strip()
        github_down = code(BUGS, """
            class GitHubDown:
                etag = ""

                def login(self):
                    return "tester"

                def find(self, fp):
                    return []

                def create_issue(self, title, body):
                    (DIR / "sending").touch()
                    end = time.time() + 15
                    while not (DIR / "dropped").exists() and time.time() < end:
                        time.sleep(0.005)
                    raise RuntimeError("GitHub is down")
            """)
        drop = """
            end = time.time() + 30
            while not (DIR / "sending").exists() and time.time() < end:
                time.sleep(0.002)
            bugs.drop(ARGS["item"])
            (DIR / "dropped").touch()
            """
        out = self.race([("sync", github_down, "print(json.dumps(bugs.flush(GitHubDown())))", {}),
                         ("drop", BUGS, drop, {"item": item})])
        self.assertEqual(json.loads(out["sync"])["failed"], 1, "the send did not fail as GitHub did")
        self.assertFalse((self.state / "bugs" / "outbox" / f"{item}.json").exists(), "the dropped report came back")


class SetAsideNoteTests(unittest.TestCase):
    # The other side of the notes' merge: a pull that cannot merge a note's hand-written text keeps the remote's
    # note and saves this Mac's copy in the state folder (work._save_note_conflict). The registry's issues say so,
    # naming each note, so the dashboard and each named project's `dev status` show it until the copies are deleted.
    # In this process: devkit's state and handoff repository in a temp folder, no project discovered, nothing
    # reaches GitHub.
    def setUp(self):
        from unittest import mock
        from devkit import audit, core, registry, work
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        state, handoff = root / "state", root / "handoff"
        for patch in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state),
                      mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(registry, "HANDOFF", handoff),
                      mock.patch.object(work, "HANDOFF", handoff), mock.patch.object(audit, "AUDIT", state / "audit"),
                      mock.patch.object(registry, "discover", side_effect=lambda: ([], [], [])),  # a new list each build
                      mock.patch.object(registry, "live_sessions", return_value=[]),
                      mock.patch.object(core, "github_user", return_value="ana"),
                      mock.patch("devkit.providers.cached", return_value={}),
                      mock.patch("devkit.ui.warn")):
            patch.start()
            self.addCleanup(patch.stop)
        self.folder = state / "handoff-conflicts"

    def handoff_issues(self) -> list:
        from devkit import registry
        return [i for i in registry.build(with_prs=False)["issues"] if i[0] == "handoff"]

    def test_notes_set_aside_by_a_sync_are_named_until_their_copies_are_deleted(self):
        from devkit import cli, core, work
        self.assertEqual(self.handoff_issues(), [])
        work._save_note_conflict("projects/acme/widget.md", "# widget\nbase\n", "# widget\nmine\n")
        work._save_note_conflict("initiatives/payments/README.md", "# payments\nbase\n", "# payments\nmine\n")
        want = ("2 handoff notes were set aside after a sync conflict (acme/widget, initiatives/payments): "
                f"{core.tilde(self.folder)} (git merge-file brings your changes back; delete each copy once merged)")
        self.assertEqual(self.handoff_issues(), [("handoff", want)])
        status = cli._issue_needs({"issues": self.handoff_issues()}, {"id": "acme/widget"})
        self.assertEqual([n["kind"] for n in status], ["handoff"], "the project's dev status does not show it")
        for f in self.folder.rglob("*.md"):
            f.unlink()
        self.assertEqual(self.handoff_issues(), [])


if __name__ == "__main__":
    unittest.main()
