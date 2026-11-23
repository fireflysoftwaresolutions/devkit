# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# One rule for every close and every stop of a step: did a lane that ran it, or the tech lead, work on it after a
# time? Work counts only when it is the step's: a file of its scope in a checkout of the project, a file its lanes
# edited for it, or a progress record that names it. The tech lead's close, an approval's ✓ and a review's ✓ of
# unverified work its author redid since, the author's own close of its unverified step, and the stop of a lane whose
# step was closed (by another, or by the lane itself) all ask it. Progress state in a temporary folder
# (test_progress.TempState), with a real git checkout as the session's folder, so a repository-relative scope names
# files in it.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_review_rework -v
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, gates_passed, launch, repo_with_worktrees  # noqa: E402

TANK, SER = "Backend engineer (Tank)", "Code reviewer (Seraph)"


def report(role, task, check):
    return (f"**[Firefly> DevKit · {role}]** api · `main` · {task}\n- **Done:** {task}\n- **Check:** {check}\n"
            "- **Next:** the tech lead merges")


class Base(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = repo_with_worktrees(tmp.name)[0]  # the session works in this checkout of acme/api
        sess = json.loads(self.state.sess.read_text())
        self.state.sess.write_text(json.dumps(dict(sess, top=str(self.repo))))
        self.t0 = time.time() - 7200

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def st(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name)

    def stop(self, aid, kind, text, at):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": text}, False, now=at)

    def edit(self, by, path, at):
        # an edit tool's call (Write) of `path`, a path in the checkout unless it is absolute, by lane `by` or main
        full = path if path.startswith("/") else str(self.repo / path)
        call = {"session_id": "s1", "tool_name": "Write", "tool_use_id": f"t{at}", "tool_input": {"file_path": full}}
        pg.note_edit("claude", dict(call, agent_id=by) if by != "main" else call, now=at)

    def approved(self, name):
        return [a["by"] for a in self.run_().get("approvals") or [] if name in (a.get("steps") or [])]

    def told(self, words):
        return [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main" and words in m["text"]]

    def end_review(self, at=30):
        gates_passed("a_ser", at=self.m(at - 1))
        self.stop("a_ser", "devkit:seraph", report(SER, "review-x", "✓ approve: read it"), self.m(at))


class LaneWorkAfterACloseTests(Base):
    def test_a_close_the_runs_history_no_longer_holds_still_names_who_closed_it(self):
        # the tech lead closes x at m15, then the run's history moves on by 200 changes of his wrap step: who closed x
        # was read from that history, so the lane's rewrite of src/x.py at m25 left x done
        pg.start("Fix", plan=["x=30m [backend]", "wrap=5m"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x", at=self.m(1))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        for k in range(100):
            pg.step("wrap", which="wrap", check="✓ wrapped", now=self.m(16) + 2 * k, force=True)
            pg.reopen("wrap", why="again", now=self.m(16) + 2 * k + 1)
        self.assertFalse([h for h in self.run_()["history"] if h.get("step") == "x"])
        self.edit("a_tank", "src/x.py", self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "⚠ 2 of 3 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "unverified")


if __name__ == "__main__":
    unittest.main()
