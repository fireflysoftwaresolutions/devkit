"""A crew member's own folder in the session scratchpad is created when its lane opens.

The brief names scratchpad/<member id>/ and the guard lets a member write only there, but a read-only member may not
create a folder, so devkit makes it at SubagentStart."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import hooks, progress  # noqa: E402

SID = "0b9d6f1e-5c1a-4f7e-9d2b-3a4c5d6e7f80"


class ScratchFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude_tmp = Path(self.tmp.name) / "claude-501"
        self.pad = self.claude_tmp / "-Users-dev-Development-acme-api" / SID / "scratchpad"
        self.data = {"session_id": SID, "agent_id": "a1b2c3",
                     "transcript_path": f"/Users/dev/.claude/projects/-Users-dev-Development-acme-api/{SID}.jsonl"}

    def start(self, persona, pad_exists=True):
        if pad_exists:
            self.pad.mkdir(parents=True)
        run = {"agents": {"a1b2c3": {"persona": persona}}}
        with mock.patch.object(hooks, "_claude_tmp", return_value=self.claude_tmp), \
                mock.patch.object(progress, "load_run", return_value=run):
            hooks._make_scratch_folder("claude", self.data)

    def test_a_crew_member_gets_its_own_folder_when_its_lane_opens(self):
        self.start("Agent Smith")
        self.assertTrue((self.pad / "smith").is_dir())

    def test_a_helper_that_is_no_crew_member_gets_no_folder(self):
        self.start("")
        self.assertEqual(list(self.pad.iterdir()), [])

    def test_nothing_is_created_when_the_session_has_no_scratchpad(self):
        self.start("Agent Smith", pad_exists=False)
        self.assertFalse(self.claude_tmp.exists())


if __name__ == "__main__":
    unittest.main()
